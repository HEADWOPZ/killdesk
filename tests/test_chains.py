import asyncio

from killdesk.chains.bsc import extract_source, source_is_honeypot
from killdesk.chains.bsc import load as load_bsc
from killdesk.chains.holders import concentration
from killdesk.chains.robinhood import load as load_robinhood
from killdesk.chains.solana import load as load_solana
from killdesk.config import Settings
from killdesk.http import SourceError
from killdesk.sources import BROWSER_UA, LiveSources


def test_concentration_excludes_the_largest_account() -> None:
    facts = concentration([400_000_000, 40_000_000, 20_000_000], 1_000_000_000)
    assert facts["top1_pct"] == 0.4
    assert facts["top1_ex_largest_pct"] == 0.04
    alone = concentration([1_000], 1_000)
    assert alone["top1_ex_largest_pct"] == 1.0


def test_solana_loader_reads_authorities_and_holders() -> None:
    class Source:
        async def solana_rpc(self, method, params):
            mint = params[0]
            assert mint == "Mint"
            if method == "getAccountInfo":
                return {
                    "value": {
                        "data": {
                            "parsed": {
                                "type": "mint",
                                "info": {
                                    "mintAuthority": None,
                                    "freezeAuthority": "Freeze",
                                    "supply": "1000",
                                    "decimals": 6,
                                },
                            }
                        }
                    }
                }
            if method == "getTokenSupply":
                return {"value": {"amount": "1000", "decimals": 6}}
            if method == "getTokenLargestAccounts":
                return {
                    "value": [
                        {"address": "lp", "amount": "600"},
                        {"address": "whale", "amount": "300"},
                    ]
                }
            raise AssertionError(method)

    facts = asyncio.run(load_solana(Source(), "Mint"))
    assert facts["chain_ok"] is True
    assert facts["mint_authority_open"] is False
    assert facts["freeze_authority_open"] is True
    assert facts["top1_ex_largest_pct"] == 0.3


def test_solana_rpc_failure_does_not_raise() -> None:
    class Source:
        async def solana_rpc(self, method, params):
            raise SourceError("solana", "429")

    facts = asyncio.run(load_solana(Source(), "Mint"))
    assert facts["chain_ok"] is False


def test_bsc_honeypot_is_a_source_comparison() -> None:
    assert source_is_honeypot("mapping(address => bool) public isBlacklisted;")
    assert not source_is_honeypot("contract Clean { function transfer() public {} }")
    body = {
        "status": "1",
        "result": [{"SourceCode": "function addBlacklist(address a) public {}", "ABI": "[]"}],
    }
    source, verified = extract_source(body)
    assert verified and source_is_honeypot(source)
    empty, verified_empty = extract_source(
        {"status": "1", "result": [{"SourceCode": "", "ABI": "Contract source code not verified"}]}
    )
    assert empty == "" and verified_empty is False


def test_bsc_without_a_key_degrades() -> None:
    class Source:
        async def evm_rpc(self, chain, method, params):
            raise SourceError("bsc", "down")

        async def etherscan(self, chain, params):
            raise AssertionError("etherscan should not be called")

    facts = asyncio.run(load_bsc(Source(), "0xabc", None))
    assert facts["etherscan_skipped"] is True
    assert facts["chain_ok"] is True
    assert any("skipped" in note for note in facts["notes"])


def test_robinhood_owner_and_chain_id() -> None:
    class Source:
        async def evm_rpc(self, chain, method, params):
            assert chain == "robinhood"
            if method == "eth_chainId":
                return "0x1237"
            if method == "eth_getCode":
                return "0x60016000"
            if method == "eth_call":
                assert params[0]["data"] == "0x8da5cb5b"
                return "0x" + "11" * 32
            raise AssertionError(method)

    facts = asyncio.run(load_robinhood(Source(), "0xabc"))
    assert facts["chain_ok"] is True
    assert facts["owner_open"] is True
    assert facts["no_contract"] is False
    assert facts["code_bytes"] == 4


def test_robinhood_live_source_sends_a_browser_user_agent() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == BROWSER_UA
        assert "Mozilla" in request.headers["user-agent"]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1237"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            sources = LiveSources(client, Settings())
            return await sources.evm_rpc("robinhood", "eth_chainId", [])

    assert asyncio.run(run()) == "0x1237"
