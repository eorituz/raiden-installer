from typing import Optional, List

from eth_utils import to_checksum_address

from web3 import Web3

from raiden_installer.account import Account
from raiden_installer.kyber.web3 import contracts as kyber_contracts, tokens as kyber_tokens
from raiden_installer.network import Network
from raiden_installer.tokens import EthereumAmount, TokenAmount, TokenTicker, Wei
from raiden_installer.uniswap.web3 import contracts as uniswap_contracts
from raiden_installer.utils import estimate_gas, send_raw_transaction


class ExchangeError(Exception):
    pass


class Exchange:
    GAS_REQUIRED = 0
    SUPPORTED_NETWORKS: List[str] = []
    TRANSFER_WEBSITE_URL: Optional[str] = None
    MAIN_WEBSITE_URL: Optional[str] = None
    TERMS_OF_SERVICE_URL: Optional[str] = None

    def __init__(self, w3: Web3):
        self.w3 = w3

    @property
    def chain_id(self):
        return int(self.w3.net.version)

    @property
    def network(self):
        return Network.get_by_chain_id(self.chain_id)

    @property
    def is_mainnet(self):
        return self.network.name == "mainnet"

    @property
    def name(self):
        return self.__class__.__name__

    def get_current_rate(self, token_amount: TokenAmount) -> EthereumAmount:
        raise NotImplementedError

    def calculate_transaction_costs(
        self, token_amount: TokenAmount, account: Account
    ) -> Optional[dict]:
        if not self.is_listing_token(token_amount.ticker) or token_amount.as_wei <= 0:
            return None

        return self._calculate_transaction_costs(token_amount, account)

    def _calculate_transaction_costs(self, token_amount: TokenAmount, account: Account) -> dict:
        raise NotImplementedError

    def buy_tokens(self, account: Account, token_amount: TokenAmount):
        raise NotImplementedError

    def is_listing_token(self, ticker: TokenTicker):
        return False

    @classmethod
    def get_by_name(cls, name):
        return {"kyber": Kyber, "uniswap": Uniswap}[name.lower()]


class Kyber(Exchange):
    GAS_REQUIRED = 500_000
    SUPPORTED_NETWORKS = ["ropsten", "mainnet"]
    MAIN_WEBSITE_URL = "https://kyber.network"
    TRANSFER_WEBSITE_URL = "https://kyberswap.com/transfer/eth"
    TERMS_OF_SERVICE_URL = "https://kyber.network/terms-and-conditions"

    def __init__(self, w3: Web3):
        super().__init__(w3=w3)
        self.network_contract_proxy = kyber_contracts.get_network_contract_proxy(self.w3)

    def is_listing_token(self, ticker: TokenTicker):
        token_network_address = self.get_token_network_address(ticker)
        return token_network_address is not None

    def get_token_network_address(self, ticker: TokenTicker):
        try:
            token_network_address = kyber_tokens.get_token_network_address(self.chain_id, ticker)
            return token_network_address and to_checksum_address(token_network_address)
        except KeyError:
            return None

    def get_current_rate(self, token_amount: TokenAmount) -> EthereumAmount:
        eth_address = to_checksum_address(
            kyber_tokens.get_token_network_address(self.chain_id, TokenTicker("ETH"))
        )

        token_network_address = to_checksum_address(
            kyber_tokens.get_token_network_address(self.chain_id, token_amount.ticker)
        )

        expected_rate, slippage_rate = self.network_contract_proxy.functions.getExpectedRate(
            token_network_address, eth_address, token_amount.as_wei
        ).call()

        if expected_rate == 0 or slippage_rate == 0:
            raise ExchangeError("Trade not possible at the moment due to lack of liquidity")

        return EthereumAmount(Wei(max(expected_rate, slippage_rate)))

    def _calculate_transaction_costs(self, token_amount: TokenAmount, account: Account) -> dict:
        exchange_rate = self.get_current_rate(token_amount)
        eth_sold = EthereumAmount(token_amount.value * exchange_rate.value)
        max_gas_price = min(
            self.w3.eth.gasPrice, self.network_contract_proxy.functions.maxGasPrice().call()
        )
        gas_price = EthereumAmount(Wei(max_gas_price))
        token_network_address = self.get_token_network_address(token_amount.ticker)
        transaction_params = {"from": account.address, "value": eth_sold.as_wei}

        gas = estimate_gas(
            self.w3,
            account,
            self.network_contract_proxy.functions.swapEtherToToken,
            token_network_address,
            exchange_rate.as_wei,
            **transaction_params,
        )

        gas_cost = EthereumAmount(Wei(gas * gas_price.as_wei))
        total = EthereumAmount(gas_cost.value + eth_sold.value)
        return {
            "gas_price": gas_price,
            "gas": gas,
            "eth_sold": eth_sold,
            "total": total,
            "exchange_rate": exchange_rate,
        }

    def buy_tokens(self, account: Account, token_amount: TokenAmount):
        if self.network.name not in self.SUPPORTED_NETWORKS:
            raise ExchangeError(
                f"{self.name} does not list {token_amount.ticker} on {self.network.name}"
            )

        transaction_costs = self.calculate_transaction_costs(token_amount, account)
        if transaction_costs is None:
            raise ExchangeError("Failed to get transactions costs")

        eth_to_sell = transaction_costs["eth_sold"]
        exchange_rate = transaction_costs["exchange_rate"]
        gas_price = transaction_costs["gas_price"]
        gas = transaction_costs["gas"]

        transaction_params = {
            "from": account.address,
            "gas_price": gas_price.as_wei,
            "gas": gas,
            "value": eth_to_sell.as_wei,
        }

        return send_raw_transaction(
            self.w3,
            account,
            self.network_contract_proxy.functions.swapEtherToToken,
            self.get_token_network_address(token_amount.ticker),
            exchange_rate.as_wei,
            **transaction_params,
        )


class Uniswap(Exchange):
    GAS_REQUIRED = 75_000
    RAIDEN_EXCHANGE_ADDRESSES = {"mainnet": "0x7D03CeCb36820b4666F45E1b4cA2538724Db271C"}
    SAI_EXCHANGE_ADDRESSES = {
        "kovan": "0x8779C708e2C3b1067de9Cd63698E4334866c691C",
        "rinkeby": "0x77dB9C915809e7BE439D2AB21032B1b8B58F6891",
    }
    EXCHANGE_FEE = 0.003
    EXCHANGE_TIMEOUT = 20 * 60  # maximum waiting time in seconds
    TRANSFER_WEBSITE_URL = "https://uniswap.ninja/send"
    MAIN_WEBSITE_URL = "https://uniswap.io"
    TERMS_OF_SERVICE_URL = "https://uniswap.io"

    def _get_exchange_proxy(self, token_ticker):
        try:
            return self.w3.eth.contract(
                abi=uniswap_contracts.UNISWAP_EXCHANGE_ABI,
                address=self._get_exchange_address(token_ticker),
            )
        except ExchangeError:
            return None

    def is_listing_token(self, ticker: TokenTicker):
        try:
            self._get_exchange_address(ticker)
            return True
        except ExchangeError:
            return False

    def _get_exchange_address(self, token_ticker: TokenTicker) -> str:
        try:
            exchanges = {"RDN": self.RAIDEN_EXCHANGE_ADDRESSES, "SAI": self.SAI_EXCHANGE_ADDRESSES}
            return exchanges[token_ticker][self.network.name]
        except KeyError:
            raise ExchangeError(f"{self.name} does not have a listed exchange for {token_ticker}")

    def _calculate_transaction_costs(self, token_amount: TokenAmount, account: Account) -> dict:
        exchange_rate = self.get_current_rate(token_amount)
        eth_sold = EthereumAmount(token_amount.value * exchange_rate.value)
        gas_price = EthereumAmount(Wei(self.w3.eth.gasPrice))
        exchange_proxy = self._get_exchange_proxy(token_amount.ticker)
        latest_block = self.w3.eth.getBlock("latest")
        deadline = latest_block.timestamp + self.EXCHANGE_TIMEOUT
        transaction_params = {"from": account.address, "value": eth_sold.as_wei}

        gas = estimate_gas(
            self.w3,
            account,
            exchange_proxy.functions.ethToTokenSwapOutput,
            token_amount.as_wei,
            deadline,
            **transaction_params,
        )

        gas_cost = EthereumAmount(Wei(gas * gas_price.as_wei))
        total = EthereumAmount(gas_cost.value + eth_sold.value)
        return {
            "gas_price": gas_price,
            "gas": gas,
            "eth_sold": eth_sold,
            "total": total,
            "exchange_rate": exchange_rate,
        }

    def buy_tokens(self, account: Account, token_amount: TokenAmount):
        costs = self.calculate_transaction_costs(token_amount, account)

        if costs is None:
            raise ExchangeError("Failed to get transaction costs")

        exchange_proxy = self._get_exchange_proxy(token_amount.ticker)
        latest_block = self.w3.eth.getBlock("latest")
        deadline = latest_block.timestamp + self.EXCHANGE_TIMEOUT
        gas = costs["gas"]
        gas_price = costs["gas_price"]
        transaction_params = {
            "from": account.address,
            "value": costs["total"].as_wei,
            "gas": 2 * gas,  # estimated gas sometimes is not enough
            "gas_price": gas_price.as_wei,
        }

        return send_raw_transaction(
            self.w3,
            account,
            exchange_proxy.functions.ethToTokenSwapOutput,
            token_amount.as_wei,
            deadline,
            **transaction_params,
        )

    def get_current_rate(self, token_amount: TokenAmount) -> EthereumAmount:
        exchange_proxy = self._get_exchange_proxy(token_amount.ticker)

        eth_to_sell = EthereumAmount(
            Wei(exchange_proxy.functions.getEthToTokenOutputPrice(token_amount.as_wei).call())
        )
        return EthereumAmount(eth_to_sell.value / token_amount.value)
