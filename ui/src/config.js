// Where the UI reads and pays. Pick one in the header (it sets ?net= in the URL).
// local: `make serve` with no contract, so nothing is paid and there is no chain feed.
export const NETWORKS = {
  testnet: {
    contract: "blindfold.ryan-tobin.testnet",
    engine: "https://87eb46e50a2a931b1f84dfeff6cb89bf77e9cff0-8000.dstack-pha-prod5.phala.network",
    rpc: "https://test.rpc.fastnear.com",
    explorer: "https://testnet.nearblocks.io",
    compose: "docker-compose.tee.yml",
  },
  mainnet: {
    contract: "blindfold.ryan-tobin.near",
    engine: "https://ad57f4701c05bfdc2fe85c8afbaf8d046499a8d5-8000.dstack-pha-prod9.phala.network",
    rpc: "https://free.rpc.fastnear.com",
    explorer: "https://nearblocks.io",
    compose: "docker-compose.mainnet.yml",
  },
  local: {
    contract: null,
    engine: import.meta.env.VITE_LOCAL_ENGINE || "http://127.0.0.1:8000",
    rpc: null,
    explorer: null,
    compose: null,
  },
};

export const DATASET = "riverside";

export function currentNetwork() {
    const name = new URLSearchParams(location.search).get("net");
    return NETWORKS[name] ? name : "testnet";
}