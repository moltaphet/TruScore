/**
 * Deployment script for contracts/truscore_oracle.py.
 *
 * `genlayer deploy` runs this file with a client already bound to the network
 * selected by `genlayer network`; the script holds no key. The fee deposit is
 * derived from the chain's live fee policy: passing no `fees` resolves the
 * deposit to zero and hosted Studio networks reject it with
 * FeeValueMustBeNonZero(1).
 */

import { readFileSync } from "fs";
import path from "path";

const CONTRACT_PATH = "contracts/truscore_oracle.py";

// Constructor arguments (wei): min_stake = 100 GEN, challenge_bond = 5 GEN.
const MIN_STAKE = 100n * 10n ** 18n;
const CHALLENGE_BOND = 5n * 10n ** 18n;

// The consensus round is slow by design: a deploy waits for the transaction to
// be decided, not merely submitted.
const RETRIES = 200;

// Evidence host whitelisted right after deployment. The deployer is the governor,
// and a fresh contract rejects every data_url until a domain is listed.
const INITIAL_DOMAIN = "api.example.com";

interface DeployReceipt {
  status?: number | string;
  statusName?: string;
  data?: { contract_address?: string };
  txDataDecoded?: { contractAddress?: string };
}

/**
 * The SDK's policy-derived fee preset. `feeValue` is the deposit the chain
 * charges up front; `distribution` is how it is allocated across the round.
 * Carried opaquely -- this script never inspects or recomputes either.
 */
interface TransactionFees {
  distribution: Record<string, unknown>;
  messageAllocations?: unknown;
  feeValue: bigint;
}

interface DeployClient {
  writeContract(input: {
    address: string;
    functionName: string;
    args: unknown[];
    value: bigint;
    fees?: TransactionFees;
  }): Promise<string>;
  initializeConsensusSmartContract(): Promise<void>;
  estimateTransactionFees(input: Record<string, never>): Promise<TransactionFees>;
  deployContract(input: {
    code: Uint8Array;
    args: unknown[];
    fees?: TransactionFees;
  }): Promise<string>;
  waitForTransactionReceipt(input: {
    hash: string;
    waitUntil: "decided" | "finalized";
    retries: number;
  }): Promise<DeployReceipt>;
}

/**
 * A decided deploy is not automatically a successful one -- a contract whose
 * `__init__` reverts still produces a decided transaction. Status 5 and 7 are
 * the accepted/finalized protocol codes; the named forms cover chains that
 * report the lifecycle name instead of the number.
 */
function isSuccessfulDeploymentReceipt(receipt: DeployReceipt): boolean {
  const numericStatus = Number(receipt.status);
  return (
    numericStatus === 5 ||
    numericStatus === 7 ||
    receipt.statusName === "ACCEPTED" ||
    receipt.statusName === "FINALIZED"
  );
}

function contractAddressFrom(receipt: DeployReceipt): string | undefined {
  // Decoded transaction data is the shape hosted networks return and the
  // simulator does not.
  const decoded = receipt.txDataDecoded?.contractAddress;
  if (decoded) return decoded;

  // `data` is where the simulator puts it, and where this CLI puts it on hosted
  // networks too. Checked for both chain types, because the deploy itself has
  // already succeeded by this point: a missed field here reports a failure for
  // a deploy that worked.
  const fromData = receipt.data?.contract_address;
  if (fromData) return fromData;

  // Last resort, so a future receipt shape is reported rather than silently
  // swallowed as "no address".
  const raw = receipt as unknown as Record<string, unknown>;
  const direct = raw.contractAddress ?? raw.contract_address;
  return typeof direct === "string" ? direct : undefined;
}

export default async function main(client: DeployClient): Promise<string> {
  const filePath = path.resolve(process.cwd(), CONTRACT_PATH);
  const code = new Uint8Array(readFileSync(filePath));

  // Registers the consensus contract's address and ABI on the client. Without
  // it the deploy has nowhere to be sent.
  await client.initializeConsensusSmartContract();

  // Studio Next has no fee manager, so the deposit must come from the chain's
  // live fee policy. Passing no `fees` resolves the deposit to zero and the
  // chain rejects the deploy with FeeValueMustBeNonZero(1).
  const fees = await client.estimateTransactionFees({});

  const hash = await client.deployContract({ code, args: [MIN_STAKE, CHALLENGE_BOND], fees });
  const receipt = await client.waitForTransactionReceipt({
    hash,
    waitUntil: "decided",
    retries: RETRIES,
  });

  if (!isSuccessfulDeploymentReceipt(receipt)) {
    throw new Error(`Deployment failed. Receipt: ${JSON.stringify(receipt)}`);
  }

  const address = contractAddressFrom(receipt);
  if (!address) {
    throw new Error(
      `Deployment receipt carried no contract address. Receipt: ${JSON.stringify(receipt)}`,
    );
  }

  console.log(`TruScore deployed at ${address}`);

  // Make the contract usable immediately: the deployer is the governor.
  const whitelistHash = await client.writeContract({
    address,
    functionName: "whitelist_domain",
    args: [INITIAL_DOMAIN],
    value: 0n,
    fees: await client.estimateTransactionFees({}),
  });
  const whitelistReceipt = await client.waitForTransactionReceipt({
    hash: whitelistHash,
    waitUntil: "decided",
    retries: RETRIES,
  });
  if (!isSuccessfulDeploymentReceipt(whitelistReceipt)) {
    throw new Error(
      `Deployed at ${address}, but whitelist_domain("${INITIAL_DOMAIN}") failed. Receipt: ${JSON.stringify(whitelistReceipt)}`,
    );
  }
  console.log(`Whitelisted domain ${INITIAL_DOMAIN}`);
  return address;
}
