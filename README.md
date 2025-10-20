УСТАНОВКА

Необходимые библиотеки:  pip install web3 python-dotenv

Файлы: 
  .env (ETH_RPC=...)  - по желанию можно вставить свою RPC; 
  keys.txt - вставляем по одному приватнику в строке.

Конфиг в ff_deposit.py:
PRIORITY_GWEI = 1.5   # начальный maxPriorityFeePerGas в gwei
MAX_WAIT      = 60   # секунд ожидания квитанции перед RBF
MAX_RETRIES   = 3     # сколько раз делать RBF
BUMP_PCT      = 20    # повышение комиссий при RBF в %
