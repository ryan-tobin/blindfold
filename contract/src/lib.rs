//! Blindfold contract: the public ledger for datasets, payments and receipts.
//!
//! Money: buyers deposit USDC with ft_transfer_call, set spending caps with a
//! full-access key, then hand their agent a function-call key that can only
//! call request_query. Each request moves the price into escrow. The enclave's
//! own account settles it: answered pays the owner and charges the privacy
//! budget, refused refunds the buyer. Unsettled requests can be expired by
//! anyone after expiry_blocks, which also refunds.
//!
//! Units: money is USDC's smallest unit (6 decimals, so $0.25 = 250000).
//! Epsilon is in millionths (1.0 = 1000000), so budgets add up exactly.

use near_sdk::json_types::{U128, U64};
use near_sdk::serde_json::{self, json, Value};
use near_sdk::store::{IterableMap, LookupMap, Vector};
use near_sdk::{
    assert_one_yocto, env, ext_contract, near, require, AccountId, Gas, NearToken, PanicOnDefault,
    PromiseError, PromiseOrValue,
};

#[cfg(test)]
mod tests;

const DAY_MS: u64 = 86_400_000;
const GAS_FT_TRANSFER: Gas = Gas::from_tgas(10);
const GAS_CALLBACK: Gas = Gas::from_tgas(10);
const RECENT_SCAN: u64 = 500;

#[near(serializers = [borsh, json])]
#[derive(Clone)]
pub struct Dataset {
    pub id: String,
    pub owner: AccountId,
    pub schema_hash: String,
    pub bundle_hash: String,
    pub price: U128,
    pub epsilon_total: u64,
    pub epsilon_spent: u64,
    /// Epsilon held by pending requests, so they can't overspend together.
    pub epsilon_pending: u64,
    pub active: bool,
}

#[near(serializers = [borsh, json])]
#[derive(Clone)]
pub struct Enclave {
    pub account_id: AccountId,
    pub signer_pubkey: String,
    pub encryption_pubkey: String,
    pub compose_hash: String,
    pub quote_hash: String,
    pub registered_at: U64,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Default)]
pub struct Balance {
    pub amount: U128,
    pub per_query_max: U128,
    pub daily_max: U128,
    pub spent_today: U128,
    pub day: u64,
}

#[near(serializers = [borsh, json])]
#[serde(rename_all = "snake_case")]
#[derive(Clone, PartialEq, Debug)]
pub enum Status {
    Pending,
    Answered,
    Refused,
    Expired,
}

#[near(serializers = [borsh, json])]
#[derive(Clone)]
pub struct QueryRequest {
    pub id: U64,
    pub buyer: AccountId,
    pub dataset_id: String,
    pub spec_hash: String,
    pub epsilon: u64,
    pub escrow: U128,
    pub status: Status,
    pub created_at: U64,
    pub created_height: U64,
    pub receipt_hash: Option<String>,
    pub epsilon_charged: u64,
    pub refusal: Option<String>,
}

#[ext_contract(ext_ft)]
#[allow(dead_code)]
trait FungibleToken {
    fn ft_transfer(&mut self, receiver_id: AccountId, amount: U128, memo: Option<String>);
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Blindfold {
    admin: AccountId,
    usdc: AccountId,
    expiry_blocks: u64,
    enclave: Option<Enclave>,
    datasets: IterableMap<String, Dataset>,
    balances: LookupMap<AccountId, Balance>,
    requests: Vector<QueryRequest>,
}

#[near]
impl Blindfold {
    #[init]
    pub fn new(admin: AccountId, usdc_contract: AccountId, expiry_blocks: u64) -> Self {
        require!(expiry_blocks > 0, "expiry_blocks must be positive");
        Self {
            admin,
            usdc: usdc_contract,
            expiry_blocks,
            enclave: None,
            datasets: IterableMap::new(b"d"),
            balances: LookupMap::new(b"b"),
            requests: Vector::new(b"r"),
        }
    }

    // --- Admin and data owners ------------------------------------------------

    /// Record the enclave. Only its account can settle. Replacing it is public.
    pub fn register_enclave(&mut self, account_id: AccountId, signer_pubkey: String,
                            encryption_pubkey: String, compose_hash: String, quote_hash: String) {
        require!(env::predecessor_account_id() == self.admin, "only the admin");
        let enclave = Enclave {
            account_id, signer_pubkey, encryption_pubkey, compose_hash, quote_hash,
            registered_at: U64(env::block_timestamp()),
        };
        emit("enclave_registered", json!(enclave));
        self.enclave = Some(enclave);
    }

    /// List a dataset. The caller becomes its owner and gets paid per answer.
    pub fn register_dataset(&mut self, id: String, schema_hash: String, bundle_hash: String,
                            price: U128, epsilon_total: u64) -> Dataset {
        require!(valid_id(&id), "id: 1-32 characters from a-z, 0-9, _ and -");
        require!(self.datasets.get(&id).is_none(), "dataset id already taken");
        require!(is_hash(&schema_hash), "schema_hash: 64 lowercase hex characters");
        require!(bundle_hash.is_empty() || is_hash(&bundle_hash), "bundle_hash: empty or 64 hex");
        require!(epsilon_total > 0, "epsilon_total must be positive");
        let d = Dataset {
            id: id.clone(), owner: env::predecessor_account_id(), schema_hash, bundle_hash, price,
            epsilon_total, epsilon_spent: 0, epsilon_pending: 0, active: true,
        };
        emit("dataset_registered", json!(d));
        self.datasets.insert(id, d.clone());
        d
    }

    /// Owner only. The budget can't be changed, so a spent dataset stays spent.
    pub fn update_dataset(&mut self, id: String, bundle_hash: Option<String>, price: Option<U128>,
                          active: Option<bool>) -> Dataset {
        let mut d = self.dataset(&id);
        require!(env::predecessor_account_id() == d.owner, "only the dataset owner");
        if let Some(h) = bundle_hash {
            require!(is_hash(&h), "bundle_hash: 64 lowercase hex characters");
            d.bundle_hash = h;
        }
        if let Some(p) = price {
            d.price = p;
        }
        if let Some(a) = active {
            d.active = a;
        }
        emit("dataset_updated", json!(d));
        self.datasets.insert(id, d.clone());
        d
    }

    // --- Buyers ---------------------------------------------------------------

    /// Called by the USDC contract after a buyer's ft_transfer_call.
    /// msg must be {"action":"deposit"}; anything else panics, which refunds.
    pub fn ft_on_transfer(&mut self, sender_id: AccountId, amount: U128, msg: String) -> PromiseOrValue<U128> {
        require!(env::predecessor_account_id() == self.usdc, "only USDC deposits are accepted");
        let parsed: Value = serde_json::from_str(&msg).unwrap_or(Value::Null);
        require!(parsed["action"] == "deposit", r#"msg must be {"action":"deposit"}"#);
        let mut b = self.balances.get(&sender_id).cloned().unwrap_or_default();
        b.amount = U128(b.amount.0 + amount.0);
        self.balances.insert(sender_id, b);
        PromiseOrValue::Value(U128(0)) // keep all of it
    }

    /// Caps on what the agent can spend. Needs 1 yoctoNEAR attached, which
    /// function-call keys can't do, so only the human's full key can call it.
    #[payable]
    pub fn set_limits(&mut self, per_query_max: U128, daily_max: U128) -> Balance {
        assert_one_yocto();
        let buyer = env::predecessor_account_id();
        let mut b = self.balances.get(&buyer).cloned().unwrap_or_default();
        b.per_query_max = per_query_max;
        b.daily_max = daily_max;
        self.balances.insert(buyer, b.clone());
        b
    }

    /// The agent's one method. No deposit, so a function-call key can call it.
    pub fn request_query(&mut self, dataset_id: String, spec_hash: String, epsilon: u64) -> U64 {
        require!(is_hash(&spec_hash), "spec_hash: 64 lowercase hex characters");
        require!(epsilon > 0, "epsilon must be positive");
        let mut d = self.dataset(&dataset_id);
        require!(d.active, "dataset is closed");
        let left = d.epsilon_total - d.epsilon_spent - d.epsilon_pending;
        require!(epsilon <= left, "over budget: not enough epsilon left");
        require!(self.enclave.is_some(), "no enclave registered");

        let buyer = env::predecessor_account_id();
        let price = d.price.0;
        if price > 0 {
            let mut b = self.balances.get(&buyer).cloned().unwrap_or_else(|| env::panic_str("no balance: deposit USDC first"));
            roll_day(&mut b);
            require!(price <= b.per_query_max.0, "price is above your per-query limit");
            require!(b.spent_today.0 + price <= b.daily_max.0, "daily limit reached");
            require!(price <= b.amount.0, "balance too low");
            b.amount = U128(b.amount.0 - price);
            b.spent_today = U128(b.spent_today.0 + price);
            self.balances.insert(buyer.clone(), b);
        }

        d.epsilon_pending += epsilon;
        self.datasets.insert(dataset_id.clone(), d);
        let id = U64(self.requests.len() as u64);
        let r = QueryRequest {
            id, buyer, dataset_id, spec_hash, epsilon, escrow: U128(price), status: Status::Pending,
            created_at: U64(env::block_timestamp()), created_height: U64(env::block_height()),
            receipt_hash: None, epsilon_charged: 0, refusal: None,
        };
        emit("query_requested", json!(r));
        self.requests.push(r);
        id
    }

    /// Enclave only. status is "answered" or "refused". A replayed answer
    /// charges 0 epsilon; the price is still paid.
    pub fn settle(&mut self, request_id: U64, status: Status, receipt_hash: String,
                  epsilon_charged: u64, refusal: Option<String>) -> QueryRequest {
        let enclave = self.enclave.as_ref().unwrap_or_else(|| env::panic_str("no enclave registered"));
        require!(env::predecessor_account_id() == enclave.account_id, "only the enclave can settle");
        require!(is_hash(&receipt_hash), "receipt_hash: 64 lowercase hex characters");
        let mut r = self.request(request_id);
        require!(r.status == Status::Pending, "request is not pending");
        let mut d = self.dataset(&r.dataset_id);
        d.epsilon_pending -= r.epsilon;
        match status {
            Status::Answered => {
                require!(refusal.is_none(), "an answer has no refusal");
                require!(epsilon_charged <= r.epsilon, "charged more epsilon than requested");
                d.epsilon_spent += epsilon_charged;
                self.credit(&d.owner, r.escrow.0);
                r.epsilon_charged = epsilon_charged;
            }
            Status::Refused => {
                require!(epsilon_charged == 0, "a refusal costs no epsilon");
                let rule = refusal.unwrap_or_default();
                require!(!rule.is_empty() && rule.len() <= 32, "refusal: a rule code, 1-32 characters");
                self.refund(&r);
                r.refusal = Some(rule);
            }
            _ => env::panic_str("status must be answered or refused"),
        }
        r.status = status;
        r.receipt_hash = Some(receipt_hash);
        let event = if r.status == Status::Answered { "query_answered" } else { "query_refused" };
        emit(event, json!(r));
        self.datasets.insert(d.id.clone(), d);
        self.requests.replace(request_id.0 as u32, r.clone());
        r
    }

    /// Anyone, once expiry_blocks have passed: refund a request nobody settled.
    pub fn expire(&mut self, request_id: U64) -> QueryRequest {
        let mut r = self.request(request_id);
        require!(r.status == Status::Pending, "request is not pending");
        require!(env::block_height() >= r.created_height.0 + self.expiry_blocks, "not expired yet");
        let mut d = self.dataset(&r.dataset_id);
        d.epsilon_pending -= r.epsilon;
        self.datasets.insert(d.id.clone(), d);
        self.refund(&r);
        r.status = Status::Expired;
        emit("query_expired", json!(r));
        self.requests.replace(request_id.0 as u32, r.clone());
        r
    }

    /// Buyers and owners take USDC out. 1 yoctoNEAR, so agent keys can't.
    #[payable]
    pub fn withdraw(&mut self, amount: U128) -> near_sdk::Promise {
        assert_one_yocto();
        let who = env::predecessor_account_id();
        let mut b = self.balances.get(&who).cloned().unwrap_or_default();
        require!(amount.0 > 0 && amount.0 <= b.amount.0, "amount must be between 1 and your balance");
        b.amount = U128(b.amount.0 - amount.0);
        self.balances.insert(who.clone(), b);
        ext_ft::ext(self.usdc.clone())
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(GAS_FT_TRANSFER)
            .ft_transfer(who.clone(), amount, Some("blindfold withdraw".into()))
            .then(Self::ext(env::current_account_id()).with_static_gas(GAS_CALLBACK).on_withdraw(who, amount))
    }

    /// If the transfer failed, put the money back.
    #[private]
    pub fn on_withdraw(&mut self, account_id: AccountId, amount: U128,
                       #[callback_result] result: Result<(), PromiseError>) -> bool {
        if result.is_err() {
            self.credit(&account_id, amount.0);
        }
        result.is_ok()
    }

    // --- Views ----------------------------------------------------------------

    pub fn get_config(&self) -> Value {
        json!({"admin": self.admin, "usdc_contract": self.usdc, "expiry_blocks": self.expiry_blocks,
               "requests": self.requests.len()})
    }

    pub fn get_dataset(&self, id: String) -> Option<Dataset> {
        self.datasets.get(&id).cloned()
    }

    pub fn list_datasets(&self) -> Vec<Dataset> {
        self.datasets.values().cloned().collect()
    }

    pub fn get_enclave(&self) -> Option<Enclave> {
        self.enclave.clone()
    }

    pub fn get_request(&self, id: U64) -> Option<QueryRequest> {
        self.requests.get(id.0 as u32).cloned()
    }

    pub fn get_balance(&self, account_id: AccountId) -> Balance {
        let mut b = self.balances.get(&account_id).cloned().unwrap_or_default();
        roll_day(&mut b);
        b
    }

    /// Newest first, looking back over the last 500 requests.
    pub fn list_recent(&self, dataset_id: String, limit: Option<u32>) -> Vec<QueryRequest> {
        let limit = limit.unwrap_or(20).min(100) as usize;
        let n = self.requests.len() as u64;
        (n.saturating_sub(RECENT_SCAN)..n).rev()
            .filter_map(|i| self.requests.get(i as u32))
            .filter(|r| r.dataset_id == dataset_id)
            .take(limit)
            .cloned()
            .collect()
    }
}

impl Blindfold {
    fn dataset(&self, id: &str) -> Dataset {
        self.datasets.get(id).cloned().unwrap_or_else(|| env::panic_str("unknown dataset"))
    }

    fn request(&self, id: U64) -> QueryRequest {
        self.requests.get(id.0 as u32).cloned().unwrap_or_else(|| env::panic_str("unknown request"))
    }

    fn credit(&mut self, who: &AccountId, amount: u128) {
        let mut b = self.balances.get(who).cloned().unwrap_or_default();
        b.amount = U128(b.amount.0 + amount);
        self.balances.insert(who.clone(), b);
    }

    /// Give the escrow back, and give back today's allowance if it's the same day.
    fn refund(&mut self, r: &QueryRequest) {
        if r.escrow.0 == 0 {
            return;
        }
        let mut b = self.balances.get(&r.buyer).cloned().unwrap_or_default();
        b.amount = U128(b.amount.0 + r.escrow.0);
        if b.day == r.created_at.0 / 1_000_000 / DAY_MS {
            b.spent_today = U128(b.spent_today.0.saturating_sub(r.escrow.0));
        }
        self.balances.insert(r.buyer.clone(), b);
    }
}

/// A new UTC day resets what the agent has spent today.
fn roll_day(b: &mut Balance) {
    let today = env::block_timestamp_ms() / DAY_MS;
    if b.day != today {
        b.day = today;
        b.spent_today = U128(0);
    }
}

fn is_hash(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn valid_id(s: &str) -> bool {
    (1..=32).contains(&s.len()) && s.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_' || c == b'-')
}

/// A standard NEAR event log (NEP-297), read by the UI feed and auditors.
fn emit(event: &str, data: Value) {
    let log = json!({"standard": "blindfold", "version": "1.0.0", "event": event, "data": [data]});
    env::log_str(&format!("EVENT_JSON:{log}"));
}