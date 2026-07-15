"""Direct on-chain discovery via Uniswap V2/V3 factory events.

Used for Robinhood Chain (chainId 4663) since it is brand new (launched
2026-07-01) and indexers like GeckoTerminal/DexScreener may not cover it
yet. Requires the factory contract addresses to be confirmed and set in
.env (ROBINHOOD_V2_FACTORY / ROBINHOOD_V3_FACTORY) - look them up as
verified contracts named "UniswapV2Factory"/"UniswapV3Factory" on
https://robinhoodchain.blockscout.com. Until those are filled in, this
module is a safe no-op (never fabricates addresses).
"""
import asyncio

from eth_abi import decode as abi_decode
from web3 import Web3

V2_PAIR_CREATED_SIG = "PairCreated(address,address,address,uint256)"
V3_POOL_CREATED_SIG = "PoolCreated(address,address,uint24,int24,address)"

V2_TOPIC = Web3.keccak(text=V2_PAIR_CREATED_SIG).hex()
V3_TOPIC = Web3.keccak(text=V3_POOL_CREATED_SIG).hex()

MAX_BLOCK_SPAN = 2000


def get_web3(rpc_url: str) -> Web3:
    return Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))


def _topic_to_address(topic) -> str:
    return Web3.to_checksum_address("0x" + topic.hex()[-40:])


def _decode_log(kind: str, log) -> str:
    if kind == "v2":
        pool_address, _ = abi_decode(["address", "uint256"], bytes(log["data"]))
    else:
        _, pool_address = abi_decode(["int24", "address"], bytes(log["data"]))
    return Web3.to_checksum_address(pool_address)


def _get_logs_sync(w3: Web3, factory_address: str, topic: str, from_block: int, to_block: int):
    return w3.eth.get_logs(
        {
            "address": Web3.to_checksum_address(factory_address),
            "topics": [topic],
            "fromBlock": from_block,
            "toBlock": to_block,
        }
    )


async def scan_new_pools(rpc_url: str, factories: dict, from_block: int, to_block: int) -> list[dict]:
    """factories: {"v2": address_or_empty, "v3": address_or_empty}"""
    if to_block < from_block:
        return []
    w3 = get_web3(rpc_url)
    results = []
    for kind, factory_addr in factories.items():
        if not factory_addr:
            continue
        topic = V2_TOPIC if kind == "v2" else V3_TOPIC
        span_start = from_block
        while span_start <= to_block:
            span_end = min(span_start + MAX_BLOCK_SPAN, to_block)
            try:
                logs = await asyncio.to_thread(
                    _get_logs_sync, w3, factory_addr, topic, span_start, span_end
                )
            except Exception:
                logs = []
            for log in logs:
                try:
                    token0 = _topic_to_address(log["topics"][1])
                    token1 = _topic_to_address(log["topics"][2])
                    pool_address = _decode_log(kind, log)
                except Exception:
                    continue
                results.append(
                    {
                        "kind": kind,
                        "token0": token0,
                        "token1": token1,
                        "pool_address": pool_address,
                        "block": log["blockNumber"],
                        "tx_hash": log["transactionHash"].hex(),
                    }
                )
            span_start = span_end + 1
    return results


async def get_latest_block(rpc_url: str) -> int:
    w3 = get_web3(rpc_url)
    return await asyncio.to_thread(lambda: w3.eth.block_number)
