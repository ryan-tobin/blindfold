//! Unit tests: `cargo test` in contract/. Each test runs the contract in a
//! simulated blockchain, switching who is calling with as_caller().

use super::*;
use near_sdk::test_utils::{get_logs, VMContextBuilder};
use near_sdk::testing_env;

const HASH: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const RECEIPT: &str = "2222222222222222222222222222222222222222222222222222222222222222";
const PRICE: u128 = 250_000; // $0.25
const EPS: u64 = 1_000_000; // 1.0

fn acc(name: &str) -> AccountId {
    name.parse().unwrap()
}

fn as_caller(name: &str, yocto: u128, height: u64) {
    let mut ctx = VMContextBuilder::new();
    ctx.current_account_id(acc("blindfold.testnet"))
        .predecessor_account_id(acc(name))
        .signer_account_id(acc(name))
        .attached_deposit(NearToken::from_yoctonear(yocto))
        .block_height(height)
        .block_timestamp(1_793_000_000_000_000_000 + height * 1_000_000_000);
    testing_env!(ctx.build());
}

/// Admin set up, enclave and Riverside registered, alice funded with $10 and capped.
fn setup() -> Blindfold {
    as_caller("admin.testnet", 0, 100);
    let mut c = Blindfold::new(acc("admin.testnet"), acc("usdc.testnet"), 50);
    c.register_enclave(acc("enclave.testnet"), "ed25519:sig".into(), "x25519:enc".into(), HASH.into(), HASH.into());
    as_caller("clinic.testnet", 0, 100);
    c.register_dataset("riverside".into(), HASH.into(), "".into(), U128(PRICE), 10 * EPS);
    as_caller("usdc.testnet", 0, 100);
    let _ = c.ft_on_transfer(acc("alice.testnet"), U128(10_000_000), r#"{"action":"deposit"}"#.into());
    as_caller("alice.testnet", 1, 100);
    c.set_limits(U128(PRICE), U128(4 * PRICE));
    c
}

fn ask(c: &mut Blindfold, eps: u64) -> U64 {
    as_caller("alice.testnet", 0, 101);
    c.request_query("riverside".into(), HASH.into(), eps)
}

#[test]
fn answered_pays_owner_and_spends_budget() {
    let mut c = setup();
    let id = ask(&mut c, EPS);
    assert_eq!(c.get_balance(acc("alice.testnet")).amount.0, 10_000_000 - PRICE);
    assert_eq!(c.get_dataset("riverside".into()).unwrap().epsilon_pending, EPS);

    as_caller("enclave.testnet", 0, 102);
    let r = c.settle(id, Status::Answered, RECEIPT.into(), EPS, None);
    assert_eq!(r.status, Status::Answered);
    assert_eq!(r.receipt_hash.as_deref(), Some(RECEIPT));
    let d = c.get_dataset("riverside".into()).unwrap();
    assert_eq!((d.epsilon_spent, d.epsilon_pending), (EPS, 0));
    assert_eq!(c.get_balance(acc("clinic.testnet")).amount.0, PRICE);
    assert!(get_logs().iter().any(|l| l.contains(r#""event":"query_answered""#)));
}

#[test]
fn refused_refunds_buyer_and_costs_no_budget() {
    let mut c = setup();
    let id = ask(&mut c, EPS);
    as_caller("enclave.testnet", 0, 102);
    let r = c.settle(id, Status::Refused, RECEIPT.into(), 0, Some("too_fine".into()));
    assert_eq!(r.refusal.as_deref(), Some("too_fine"));
    let b = c.get_balance(acc("alice.testnet"));
    assert_eq!((b.amount.0, b.spent_today.0), (10_000_000, 0));
    let d = c.get_dataset("riverside".into()).unwrap();
    assert_eq!((d.epsilon_spent, d.epsilon_pending), (0, 0));
    assert_eq!(c.get_balance(acc("clinic.testnet")).amount.0, 0);
    assert!(get_logs().iter().any(|l| l.contains(r#""refusal":"too_fine""#)));
}

#[test]
fn replay_answer_charges_no_epsilon_but_pays() {
    let mut c = setup();
    let id = ask(&mut c, EPS);
    as_caller("enclave.testnet", 0, 102);
    c.settle(id, Status::Answered, RECEIPT.into(), 0, None);
    assert_eq!(c.get_dataset("riverside".into()).unwrap().epsilon_spent, 0);
    assert_eq!(c.get_balance(acc("clinic.testnet")).amount.0, PRICE);
}

#[test]
#[should_panic(expected = "only the enclave can settle")]
fn only_enclave_settles() {
    let mut c = setup();
    let id = ask(&mut c, EPS);
    as_caller("alice.testnet", 0, 102);
    c.settle(id, Status::Answered, RECEIPT.into(), EPS, None);
}

#[test]
#[should_panic(expected = "charged more epsilon than requested")]
fn cannot_charge_more_than_requested() {
    let mut c = setup();
    let id = ask(&mut c, EPS / 5);
    as_caller("enclave.testnet", 0, 102);
    c.settle(id, Status::Answered, RECEIPT.into(), EPS, None);
}

#[test]
#[should_panic(expected = "request is not pending")]
fn settles_only_once() {
    let mut c = setup();
    let id = ask(&mut c, EPS);
    as_caller("enclave.testnet", 0, 102);
    c.settle(id, Status::Answered, RECEIPT.into(), EPS, None);
    c.settle(id, Status::Refused, RECEIPT.into(), 0, Some("too_fine".into()));
}

#[test]
#[should_panic(expected = "price is above your per-query limit")]
fn per_query_limit() {
    let mut c = setup();
    as_caller("alice.testnet", 1, 101);
    c.set_limits(U128(PRICE - 1), U128(4 * PRICE));
    ask(&mut c, EPS);
}

#[test]
#[should_panic(expected = "daily limit reached")]
fn daily_limit() {
    let mut c = setup();
    for _ in 0..5 {
        ask(&mut c, EPS / 10); // the 5th is over 4 x price
    }
}

#[test]
fn new_day_resets_daily_limit() {
    let mut c = setup();
    for _ in 0..4 {
        ask(&mut c, EPS / 10);
    }
    as_caller("alice.testnet", 0, 101 + 86_400); // one day of 1-second blocks later
    c.request_query("riverside".into(), HASH.into(), EPS / 10);
    assert_eq!(c.get_balance(acc("alice.testnet")).spent_today.0, PRICE);
}

#[test]
#[should_panic(expected = "Requires attached deposit of exactly 1 yoctoNEAR")]
fn agent_key_cannot_raise_limits() {
    let mut c = setup();
    as_caller("alice.testnet", 0, 101); // a function-call key attaches nothing
    c.set_limits(U128(u128::MAX), U128(u128::MAX));
}

#[test]
#[should_panic(expected = "over budget")]
fn pending_requests_reserve_budget() {
    let mut c = setup();
    as_caller("alice.testnet", 1, 101);
    c.set_limits(U128(PRICE), U128(100 * PRICE));
    for _ in 0..11 {
        ask(&mut c, EPS); // budget 10: the 11th can't fit, even unsettled
    }
}

#[test]
#[should_panic(expected = "dataset is closed")]
fn closed_dataset_refuses() {
    let mut c = setup();
    as_caller("clinic.testnet", 0, 101);
    c.update_dataset("riverside".into(), None, None, Some(false));
    ask(&mut c, EPS);
}

#[test]
#[should_panic(expected = "only the dataset owner")]
fn only_owner_updates_dataset() {
    let mut c = setup();
    as_caller("alice.testnet", 0, 101);
    c.update_dataset("riverside".into(), None, Some(U128(0)), None);
}

#[test]
fn expire_refunds_after_expiry_blocks() {
    let mut c = setup();
    let id = ask(&mut c, EPS);
    as_caller("bob.testnet", 0, 101 + 50);
    let r = c.expire(id);
    assert_eq!(r.status, Status::Expired);
    assert_eq!(c.get_balance(acc("alice.testnet")).amount.0, 10_000_000);
    assert_eq!(c.get_dataset("riverside".into()).unwrap().epsilon_pending, 0);
}

#[test]
#[should_panic(expected = "not expired yet")]
fn expire_too_early() {
    let mut c = setup();
    let id = ask(&mut c, EPS);
    as_caller("bob.testnet", 0, 101 + 49);
    c.expire(id);
}

#[test]
#[should_panic(expected = "only USDC deposits are accepted")]
fn deposit_only_from_usdc() {
    let mut c = setup();
    as_caller("fake-usdc.testnet", 0, 101);
    let _ = c.ft_on_transfer(acc("alice.testnet"), U128(1), r#"{"action":"deposit"}"#.into());
}

#[test]
#[should_panic(expected = "msg must be")]
fn deposit_needs_deposit_msg() {
    let mut c = setup();
    as_caller("usdc.testnet", 0, 101);
    let _ = c.ft_on_transfer(acc("alice.testnet"), U128(1), "hello".into());
}

#[test]
#[should_panic(expected = "dataset id already taken")]
fn dataset_ids_are_unique() {
    let mut c = setup();
    as_caller("mallory.testnet", 0, 101);
    c.register_dataset("riverside".into(), HASH.into(), "".into(), U128(0), EPS);
}

#[test]
fn list_recent_newest_first() {
    let mut c = setup();
    for _ in 0..3 {
        ask(&mut c, EPS / 10);
    }
    let ids: Vec<u64> = c.list_recent("riverside".into(), Some(2)).iter().map(|r| r.id.0).collect();
    assert_eq!(ids, vec![2, 1]);
}