#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# FF cooldown initiator (Ethereum Mainnet, chainId=1)
# - For each wallet in keys.txt, reads sFF (vault shares) balance and calls cooldownShares(full_balance, owner=wallet)
# - RBF logic with fee bumps, web3 v5/v6 compatibility helpers.
#
# Требования: pip install web3 python-dotenv
# Файлы: .env (ETH_RPC=...), keys.txt (по одному приватнику на строке)
#
# Основано на вашем скрипте депозита; упрощено под вызов cooldownShares().
#
PRIORITY_GWEI = 1.5   # начальный maxPriorityFeePerGas в gwei
MAX_WAIT      = 60    # секунд ожидания квитанции перед RBF
MAX_RETRIES   = 3     # сколько раз делать RBF
BUMP_PCT      = 20    # повышение комиссий при RBF в %

import os
import time
from decimal import Decimal
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

ETH_CHAIN_ID = 1
MIN_ETH_FOR_TX = Decimal("0.00003")  # минимальный запас (примерно), проверка перед отправкой

# Адреса (Vault = sFF ERC20 + прокси контракта)
ADDR_VAULT = Web3.to_checksum_address("0x1a0C3FfCbd101c6f2f6650DED9964c4A568C4D72")

# ABI
ABI_ERC20 = [
    {"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"owner","type":"address"}],"outputs":[{"type":"uint256"}]},
    {"name":"decimals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint8"}]},
    {"name":"symbol","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"string"}]},
]
ABI_VAULT = [
    # cooldownShares(uint256 shares, address owner)
    {"name":"cooldownShares","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"shares","type":"uint256"},{"name":"owner","type":"address"}],"outputs":[]}
]

# -------- Nonce Manager --------
class NonceManager:
    def __init__(self, w3: Web3, address: str):
        self.w3 = w3
        self.address = address
        self._nonce = self._read_pending()
    def _read_pending(self) -> int:
        return self.w3.eth.get_transaction_count(self.address, block_identifier="pending")
    def current(self) -> int:
        return self._nonce
    def next(self) -> int:
        n = self._nonce
        self._nonce += 1
        return n
    def back(self) -> None:
        self._nonce = max(0, self._nonce - 1)
    def sync(self):
        self._nonce = self._read_pending()

# -------- Utils: web3 v5/v6 compatibility --------
def signed_raw_tx_bytes(signed) -> bytes:
    raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        if isinstance(signed, dict) and "rawTransaction" in signed:
            raw = signed["rawTransaction"]
        elif isinstance(signed, dict) and "raw_transaction" in signed:
            raw = signed["raw_transaction"]
    if raw is None:
        raise AttributeError("SignedTransaction has neither rawTransaction nor raw_transaction")
    return raw

# -------- Fees helpers --------
def suggest_fees(w3: Web3, priority_gwei: Decimal) -> Dict[str, int]:
    try:
        fh = w3.eth.fee_history(3, "latest")
        base = Decimal(fh["baseFeePerGas"][-1])
    except Exception:
        base = Decimal(w3.eth.gas_price)
    priority = Web3.to_wei(float(priority_gwei), "gwei")
    max_fee = int(base + priority * 2)
    return {"maxPriorityFeePerGas": int(priority), "maxFeePerGas": max_fee}

def bump_fees(fees: Dict[str, int], bump_pct: Decimal) -> Dict[str, int]:
    mul = (Decimal(1) + Decimal(bump_pct)/Decimal(100))
    return {
        "maxPriorityFeePerGas": int(Decimal(fees["maxPriorityFeePerGas"]) * mul),
        "maxFeePerGas": int(Decimal(fees["maxFeePerGas"]) * mul),
    }

def estimate_gas_safe(w3: Web3, tx: Dict[str, Any], fallback_gas: Optional[int] = None) -> int:
    try:
        gas_est = w3.eth.estimate_gas(tx)
        return int(Decimal(gas_est) * Decimal("1.10"))
    except Exception:
        if fallback_gas is None:
            raise
        return fallback_gas

# -------- Sender with RBF --------
def send_with_rbf(w3: Web3, account, nonce_mgr: NonceManager, tx_fields: Dict[str, Any], tag: str,
                  max_wait: int, max_retries: int, bump_pct: Decimal) -> str:
    tx = dict(tx_fields)
    tx["nonce"] = nonce_mgr.next()
    tx["gas"] = estimate_gas_safe(w3, tx, fallback_gas=140000)  # немного выше, чем у deposit
    fees = {"maxPriorityFeePerGas": tx["maxPriorityFeePerGas"], "maxFeePerGas": tx["maxFeePerGas"]}
    print(f"    gas={tx['gas']} maxFeePerGas={fees['maxFeePerGas']} maxPriorityFeePerGas={fees['maxPriorityFeePerGas']}")

    attempt = 0
    while True:
        signed = account.sign_transaction(tx)
        raw = signed_raw_tx_bytes(signed)
        tx_hash = w3.eth.send_raw_transaction(raw)
        tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else Web3.to_hex(tx_hash)
        print(f"  -> {tag}: отправлено {tx_hex}")
        print(f"     ссылка: https://etherscan.io/tx/{tx_hex}")

        t0 = time.time()
        while time.time() - t0 < max_wait:
            try:
                rcpt = w3.eth.get_transaction_receipt(tx_hash)
                if rcpt is not None and hasattr(rcpt, "status"):
                    if rcpt.status == 1:
                        print(f"     {tag}: ✅ success (block={rcpt.blockNumber}, gasUsed={rcpt.gasUsed})")
                    else:
                        print(f"     {tag}: ❌ failed (status=0, block={rcpt.blockNumber})")
                    return tx_hex
            except Exception:
                pass
            time.sleep(5)

        attempt += 1
        if attempt > max_retries:
            print(f"     {tag}: ⚠️ квитанция не получена за {max_wait}s после {max_retries} RBF-попыток.")
            return tx_hex
        nonce_mgr.back()
        fees = bump_fees(fees, Decimal(BUMP_PCT))
        tx["maxPriorityFeePerGas"] = fees["maxPriorityFeePerGas"]
        tx["maxFeePerGas"]          = fees["maxFeePerGas"]
        print(f"     {tag}: RBF bump +{BUMP_PCT}% → maxFeePerGas={tx['maxFeePerGas']} maxPriority={tx['maxPriorityFeePerGas']}")

# -------- Web3 init / keys --------
def load_keys(path: str = "keys.txt"):
    keys = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                if not (s.startswith("0x") and len(s) >= 66):
                    raise ValueError(f"Неверный приватный ключ: {s[:12]}...")
                keys.append(s)
    if not keys:
        raise RuntimeError("keys.txt пуст.")
    return keys

def build_w3() -> Web3:
    load_dotenv()
    rpc = os.getenv("ETH_RPC")
    if not rpc:
        raise RuntimeError("Укажите ETH_RPC в .env")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 60}))
    try:
        chain_id = w3.eth.chain_id
    except Exception:
        chain_id = None
    if chain_id and chain_id != ETH_CHAIN_ID:
        print(f"ВНИМАНИЕ: chain_id={chain_id}, ожидается {ETH_CHAIN_ID} (Ethereum Mainnet)")
        time.sleep(1.0)
    return w3

# -------- Business logic --------
def step_cooldown_all_shares(w3: Web3, account, nonce_mgr: NonceManager, vault_addr: str, fees: Dict[str,int]) -> Optional[str]:
    sff = w3.eth.contract(address=vault_addr, abi=ABI_ERC20)     # sFF is ERC20 on the same proxy
    vault = w3.eth.contract(address=vault_addr, abi=ABI_VAULT)   # cooldownShares on the same proxy

    bal = int(sff.functions.balanceOf(account.address).call())
    print(f"  Баланс sFF (shares): {bal}")
    if bal == 0:
        print("  sFF баланс = 0 — пропускаю кошелёк")
        return None

    tx = {
        "chainId": ETH_CHAIN_ID,
        "from": account.address,
        "to": vault_addr,
        "value": 0,
        "data": vault.functions.cooldownShares(bal, account.address)._encode_transaction_data(),
        **fees
    }
    return send_with_rbf(w3, account, nonce_mgr, tx, f"vault.cooldownShares({bal}, owner={account.address})", MAX_WAIT, MAX_RETRIES, Decimal(BUMP_PCT))

def main():
    w3 = build_w3()
    keys = load_keys("keys.txt")

    print(f"Подключился к RPC; кошельков: {len(keys)}")
    print(f"Vault / sFF (proxy): {ADDR_VAULT}")

    base_fees = suggest_fees(w3, Decimal(PRIORITY_GWEI))

    for idx, pk in enumerate(keys, 1):
        acct = Account.from_key(pk)
        nonce_mgr = NonceManager(w3, acct.address)
        print(f"\n=== Wallet #{idx}: {acct.address} (start pending nonce={nonce_mgr.current()}) ===")

        eth_balance = Decimal(w3.from_wei(w3.eth.get_balance(acct.address), "ether"))
        if eth_balance < MIN_ETH_FOR_TX:
            print(f"  ⚠️ На кошельке мало ETH для газа: {eth_balance} ETH < {MIN_ETH_FOR_TX} ETH — пропуск")
            continue

        try:
            step_cooldown_all_shares(w3, acct, nonce_mgr, ADDR_VAULT, dict(base_fees))
        except Exception as e:
            print(f"=== Кошелёк {acct.address}: непредвиденная ошибка: {e} ===")
            continue

    print("\nГотово.")

if __name__ == "__main__":
    main()
