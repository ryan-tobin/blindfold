// One NEAR wallet connector per page load. Switching network reloads the page.
import { NearConnector } from "@hot-labs/near-connect";

export function makeConnector(name, net) {
  const features = { signAndSendTransaction: true };
  if (name === "testnet") features.testnet = true;
  return new NearConnector({
    network: name,
    providers: { [name]: [net.rpc] },
    features,
    footerBranding: null,
  });
}

export async function connectedAccount(connector) {
  try {
    const { accounts } = await connector.getConnectedWallet();
    return accounts[0]?.accountId || null;
  } catch {
    return null;
  }
}