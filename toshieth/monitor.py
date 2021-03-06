import asyncio
import logging
import time
from ethereum.abi import decode_abi, decode_single
from toshi.jsonrpc.client import JsonRPCClient
from toshi.jsonrpc.errors import JsonRPCError, HTTPError
from toshi.log import configure_logger, log_unhandled_exceptions
from toshi.database import prepare_database
from toshi.redis import prepare_redis, get_redis_connection
from toshi.config import config
from toshieth.tasks import manager_dispatcher, erc20_dispatcher, eth_dispatcher, collectibles_dispatcher

from toshi.utils import parse_int
from toshi.ethereum.utils import data_decoder

from .constants import TRANSFER_TOPIC, DEPOSIT_TOPIC, WITHDRAWAL_TOPIC, WETH_CONTRACT_ADDRESS
from .utils import get_transaction_log_index

DEFAULT_BLOCK_CHECK_DELAY = 0
DEFAULT_POLL_DELAY = 1
# Parity timeout is 60 seconds, this is a bit short for assuming
# the filter has died as new blocks could take longer so using
# 120 seconds as 1 minute of missing filter info is acceptable
FILTER_TIMEOUT = 120
SANITY_CHECK_CALLBACK_TIME = 10
# 5 minutes delay until reporting errors when no new blocks are seen
NEW_BLOCK_TIMEOUT = 300

UNCONFIRMED_TRANSACTIONS_REDIS_KEY = "toshieth.monitor:unconfirmed_txs"

log = logging.getLogger("toshieth.monitor")

JSONRPC_ERRORS = (HTTPError,
                  ConnectionRefusedError,  # Server isn't running
                  OSError,  # No route to host
                  JsonRPCError,  #
                 )

class BlockMonitor:

    def __init__(self):
        configure_logger(log)

        if 'monitor' in config:
            node_url = config['monitor']['url']
        else:
            log.warning("monitor using config['ethereum'] node")
            node_url = config['ethereum']['url']

        self.eth = JsonRPCClient(node_url,
                                 connect_timeout=5.0,
                                 request_timeout=10.0)
        # filter health processes depend on some of the calls failing on the first time
        # so we have a separate client to handle those
        self.filter_eth = JsonRPCClient(node_url,
                                        force_instance=True,
                                        connect_timeout=10.0,
                                        request_timeout=60.0)

        self._check_schedule = None
        self._poll_schedule = None
        self._sanity_check_schedule = None
        self._block_checking_process = None
        self._filter_poll_process = None
        self._sanity_check_process = None
        self._process_unconfirmed_transactions_process = None

        self._new_pending_transaction_filter_id = None
        self._last_saw_new_block = asyncio.get_event_loop().time()
        self._shutdown = False

        self._lastlog = 0
        self._blocktimes = []

    def start(self):
        if not hasattr(self, '_startup_future'):
            self._startup_future = asyncio.get_event_loop().create_future()
            asyncio.get_event_loop().create_task(self._initialise())
            self._sanity_check_schedule = asyncio.get_event_loop().call_later(SANITY_CHECK_CALLBACK_TIME, self.run_sanity_check)
        return self._startup_future

    @log_unhandled_exceptions(logger=log)
    async def _initialise(self):
        # prepare databases
        self.pool = await prepare_database(handle_migration=False)
        await prepare_redis()

        async with self.pool.acquire() as con:
            # check for the last non stale block processed
            row = await con.fetchrow("SELECT blocknumber FROM blocks WHERE stale = FALSE ORDER BY blocknumber DESC LIMIT 1")
            if row is None:
                # fall back on old last_blocknumber
                row = await con.fetchrow("SELECT blocknumber FROM last_blocknumber")
        if row is None:
            # if there was no previous start, get the current block number
            # and start from there
            last_block_number = await self.eth.eth_blockNumber()
            async with self.pool.acquire() as con:
                await con.execute("INSERT INTO last_blocknumber VALUES ($1)", last_block_number)
        else:
            last_block_number = row['blocknumber']

        self.last_block_number = last_block_number
        self._shutdown = False

        await self.register_filters()

        self.schedule_filter_poll()

        self._startup_future.set_result(True)

    async def register_filters(self):
        if not self._shutdown:
            await self.register_new_pending_transaction_filter()

    async def register_new_pending_transaction_filter(self):
        backoff = 0
        while not self._shutdown:
            try:
                filter_id = await self.filter_eth.eth_newPendingTransactionFilter()
                log.info("Listening for new pending transactions with filter id: {}".format(filter_id))
                self._new_pending_transaction_filter_id = filter_id
                self._last_saw_new_pending_transactions = asyncio.get_event_loop().time()
                return filter_id
            except:
                log.exception("Error registering for new pending transactions")
                if not self._shutdown:
                    backoff = min(backoff + 1, 10)
                    await asyncio.sleep(backoff)

    def schedule_block_check(self, delay=DEFAULT_BLOCK_CHECK_DELAY):
        if self._shutdown:
            return
        self._check_schedule = asyncio.get_event_loop().call_later(
            delay, self.run_block_check)

    def schedule_filter_poll(self, delay=DEFAULT_POLL_DELAY):
        if self._shutdown:
            return
        self._poll_schedule = asyncio.get_event_loop().call_later(
            delay, self.run_filter_poll)

    def run_filter_poll(self):
        if self._shutdown:
            return
        if self._filter_poll_process is not None and not self._filter_poll_process.done():
            log.debug("filter polling is already running")
            return
        self._filter_poll_process = asyncio.get_event_loop().create_task(self.filter_poll())

    def run_block_check(self):
        if self._shutdown:
            return
        if self._block_checking_process is not None and not self._block_checking_process.done():
            log.debug("Block check is already running")
            return

        self._block_checking_process = asyncio.get_event_loop().create_task(self.block_check())

    def run_process_unconfirmed_transactions(self):
        if self._shutdown:
            return
        if self._process_unconfirmed_transactions_process is not None and not self._process_unconfirmed_transactions_process.done():
            log.debug("Process unconfirmed transactions is already running")
            return

        self._process_unconfirmed_transactions_process = asyncio.get_event_loop().create_task(self.process_unconfirmed_transactions())

    @log_unhandled_exceptions(logger=log)
    async def block_check(self):
        while not self._shutdown:
            try:
                block = await self.eth.eth_getBlockByNumber(self.last_block_number + 1)
            except:
                log.exception("Failed eth_getBlockByNumber call")
                break
            if block:
                manager_dispatcher.update_default_gas_price(self.last_block_number + 1)
                self._last_saw_new_block = asyncio.get_event_loop().time()
                processing_start_time = asyncio.get_event_loop().time()
                if self._lastlog + 300 < asyncio.get_event_loop().time():
                    self._lastlog = asyncio.get_event_loop().time()
                    log.info("Processing block {}".format(block['number']))
                    if len(self._blocktimes) > 0:
                        log.info("Average processing time per last {} blocks: {}".format(len(self._blocktimes), sum(self._blocktimes) / len(self._blocktimes)))

                # check for reorg

                async with self.pool.acquire() as con:
                    last_block = await con.fetchrow("SELECT * FROM blocks WHERE blocknumber = $1", self.last_block_number)
                # if we don't have the previous block, do a quick sanity check to see if there's any blocks lower
                if last_block is None:
                    async with self.pool.acquire() as con:
                        last_block_number = await con.fetchval(
                            "SELECT blocknumber FROM blocks "
                            "WHERE blocknumber < $1 "
                            "ORDER BY blocknumber DESC LIMIT 1",
                            self.last_block_number)
                    if last_block_number:
                        log.warning("found gap in blocks @ block number: #{}".format(last_block_number + 1))
                        # roll back to the last block number and sync up
                        self.last_block_number = last_block_number
                        continue
                else:
                    # make sure hash of the last block is the same as the current hash's parent block
                    if last_block['hash'] != block['parentHash']:
                        # we have a reorg!
                        success = await self.handle_reorg()
                        if success:
                            continue
                        # if we didn't find a reorg point, continue on as normal to avoid
                        # preventing the system from operating as a whole

                # check if we're reorging
                async with self.pool.acquire() as con:
                    is_reorg = await con.fetchval("SELECT 1 FROM blocks WHERE blocknumber = $1", self.last_block_number + 1)

                if block['logsBloom'] != "0x" + ("0" * 512):
                    try:
                        logs_list = await self.eth.eth_getLogs(fromBlock=block['number'],
                                                               toBlock=block['number'])
                    except:
                        log.exception("failed eth_getLogs call")
                        break
                    logs = {}
                    for _log in logs_list:
                        if _log['transactionHash'] not in logs:
                            logs[_log['transactionHash']] = [_log]
                        else:
                            logs[_log['transactionHash']].append(_log)
                else:
                    logs_list = []
                    logs = {}

                process_tx_tasks = []
                for tx in block['transactions']:
                    # send notifications to sender and reciever
                    if tx['hash'] in logs:
                        tx['logs'] = logs[tx['hash']]
                    process_tx_tasks.append(
                        asyncio.get_event_loop().create_task(self.process_transaction(tx, is_reorg=is_reorg)))
                await asyncio.gather(*process_tx_tasks)

                if logs_list:
                    # send notifications for anyone registered
                    async with self.pool.acquire() as con:
                        for event in logs_list:
                            for topic in event['topics']:
                                filters = await con.fetch(
                                    "SELECT * FROM filter_registrations WHERE contract_address = $1 AND topic_id = $2",
                                    event['address'], topic)
                                for filter in filters:
                                    eth_dispatcher.send_filter_notification(
                                        filter['filter_id'], filter['topic'], event['data'])

                # update the latest block number, only if it is larger than the
                # current block number.
                block_number = parse_int(block['number'])
                if self.last_block_number < block_number:
                    self.last_block_number = block_number

                async with self.pool.acquire() as con:
                    await con.execute("UPDATE last_blocknumber SET blocknumber = $1 "
                                      "WHERE blocknumber < $1",
                                      block_number)
                    await con.execute("INSERT INTO blocks (blocknumber, timestamp, hash, parent_hash) "
                                      "VALUES ($1, $2, $3, $4) "
                                      "ON CONFLICT (blocknumber) DO UPDATE "
                                      "SET timestamp = EXCLUDED.timestamp, hash = EXCLUDED.hash, "
                                      "parent_hash = EXCLUDED.parent_hash, stale = FALSE",
                                      block_number, parse_int(block['timestamp']) or int(time.time()),
                                      block['hash'], block['parentHash'])

                collectibles_dispatcher.notify_new_block(block_number)
                processing_end_time = asyncio.get_event_loop().time()
                self._blocktimes.append(processing_end_time - processing_start_time)
                if len(self._blocktimes) > 100:
                    self._blocktimes = self._blocktimes[-100:]

            else:

                break

        self._block_checking_process = None

    @log_unhandled_exceptions(logger=log)
    async def filter_poll(self):

        # check for newly added erc20 tokens
        if not self._shutdown:

            async with self.pool.acquire() as con:
                rows = await con.fetch("SELECT contract_address FROM tokens WHERE ready = FALSE AND custom = FALSE")
                if len(rows) > 0:
                    total_registrations = await con.fetchval("SELECT COUNT(*) FROM token_registrations")
                else:
                    total_registrations = 0

            for row in rows:
                log.info("Got new erc20 token: {}. updating {} registrations".format(
                    row['contract_address'], total_registrations))

            if len(rows) > 0:
                limit = 1000
                for offset in range(0, total_registrations, limit):
                    async with self.pool.acquire() as con:
                        registrations = await con.fetch(
                            "SELECT eth_address FROM token_registrations OFFSET $1 LIMIT $2",
                            offset, limit)
                    for row in rows:
                        erc20_dispatcher.update_token_cache(
                            row['contract_address'],
                            *[r['eth_address'] for r in registrations])
                async with self.pool.acquire() as con:
                    await con.executemany("UPDATE tokens SET ready = true WHERE contract_address = $1",
                                          [(r['contract_address'],) for r in rows])

        if not self._shutdown:

            if self._new_pending_transaction_filter_id is not None:
                # get the list of new pending transactions
                try:
                    new_pending_transactions = await self.filter_eth.eth_getFilterChanges(self._new_pending_transaction_filter_id)
                    # add any to the list of unprocessed transactions
                    for tx_hash in new_pending_transactions:
                        await self.redis.hsetnx(
                            UNCONFIRMED_TRANSACTIONS_REDIS_KEY,
                            tx_hash, int(asyncio.get_event_loop().time()))
                except JSONRPC_ERRORS:
                    log.exception("WARNING: unable to connect to server")
                    new_pending_transactions = None

                if new_pending_transactions is None:
                    await self.register_filters()
                elif len(new_pending_transactions) > 0:
                    self._last_saw_new_pending_transactions = asyncio.get_event_loop().time()
                else:
                    # make sure the filter timeout period hasn't passed
                    time_since_last_pending_transaction = int(asyncio.get_event_loop().time() - self._last_saw_new_pending_transactions)
                    if time_since_last_pending_transaction > FILTER_TIMEOUT:
                        log.warning("Haven't seen any new pending transactions for {} seconds".format(time_since_last_pending_transaction))
                        await self.register_new_pending_transaction_filter()

                if await self.redis.hlen(UNCONFIRMED_TRANSACTIONS_REDIS_KEY) > 0:
                    self.run_process_unconfirmed_transactions()

        if not self._shutdown:

            # no need to run this if the block checking process is still running
            if self._block_checking_process is None or self._block_checking_process.done():
                try:
                    block_number = await self.filter_eth.eth_blockNumber()
                except JSONRPC_ERRORS:
                    log.exception("Error getting current block number")
                    block_number = 0
                if block_number > self.last_block_number and not self._shutdown:
                    self.schedule_block_check()

        self._filter_poll_process = None

        if not self._shutdown:
            self.schedule_filter_poll(1 if (await self.redis.hlen(UNCONFIRMED_TRANSACTIONS_REDIS_KEY) > 0) else DEFAULT_POLL_DELAY)

    @log_unhandled_exceptions(logger=log)
    async def process_unconfirmed_transactions(self):

        if self._shutdown:
            return

        # go through all the unmatched transactions that have no match
        unmatched_transactions = await self.redis.hgetall(UNCONFIRMED_TRANSACTIONS_REDIS_KEY, encoding="utf-8")
        for tx_hash, created in unmatched_transactions.items():
            age = asyncio.get_event_loop().time() - int(created)
            try:
                tx = await self.eth.eth_getTransactionByHash(tx_hash)
            except JSONRPC_ERRORS:
                log.exception("Error getting transaction")
                tx = None
            if tx is None:
                # if the tx has existed for 60 seconds and not found, assume it was
                # removed from the network before being accepted into a block
                if age >= 60:
                    await self.redis.hdel(UNCONFIRMED_TRANSACTIONS_REDIS_KEY, tx_hash)
            else:
                await self.redis.hdel(UNCONFIRMED_TRANSACTIONS_REDIS_KEY, tx_hash)

                # check if the transaction has already been included in a block
                # and if so, ignore this notification as it will be picked up by
                # the confirmed block check and there's no need to send two
                # notifications about it
                if tx['blockNumber'] is not None:
                    continue

                await self.process_transaction(tx)

            if self._shutdown:
                break

        self._process_unconfirmed_transactions_process = None

    @log_unhandled_exceptions(logger=log)
    async def process_transaction(self, transaction, is_reorg=False):

        to_address = transaction['to']
        # make sure we use a valid encoding of "empty" for contract deployments
        if to_address is None:
            to_address = "0x"
        from_address = transaction['from']

        async with self.pool.acquire() as con:
            # find if we have a record of this tx by checking the from address and nonce
            db_txs = await con.fetch("SELECT * FROM transactions WHERE "
                                     "from_address = $1 AND nonce = $2",
                                     from_address, parse_int(transaction['nonce']))
            if len(db_txs) > 1:
                # see if one has the same hash
                db_tx = await con.fetchrow("SELECT * FROM transactions WHERE "
                                           "from_address = $1 AND nonce = $2 AND hash = $3 AND (status != 'error' OR status = 'new')",
                                           from_address, parse_int(transaction['nonce']), transaction['hash'])
                if db_tx is None:
                    # find if there are any that aren't marked as error
                    no_error = await con.fetch("SELECT * FROM transactions WHERE "
                                               "from_address = $1 AND nonce = $2 AND hash != $3 AND (status != 'error' OR status = 'new')",
                                               from_address, parse_int(transaction['nonce']), transaction['hash'])
                    if len(no_error) == 1:
                        db_tx = no_error[0]
                    elif len(no_error) != 0:
                        log.warning("Multiple transactions from '{}' exist with nonce '{}' in unknown state")

            elif len(db_txs) == 1:
                db_tx = db_txs[0]
            else:
                db_tx = None

            # if we have a previous transaction, do some checking to see what's going on
            # see if this is an overwritten transaction
            # if the status of the old tx was previously an error, we don't care about it
            # otherwise, we have to notify the interested parties of the overwrite

            if db_tx and db_tx['hash'] != transaction['hash'] and db_tx['status'] != 'error':

                if db_tx['v'] is not None:
                    log.warning("found overwritten transaction!")
                    log.warning("tx from: {}".format(from_address))
                    log.warning("nonce: {}".format(parse_int(transaction['nonce'])))
                    log.warning("old tx hash: {}".format(db_tx['hash']))
                    log.warning("new tx hash: {}".format(transaction['hash']))

                manager_dispatcher.update_transaction(db_tx['transaction_id'], 'error')
                db_tx = None

            # if reorg, and the transaction is confirmed, just update which block it was included in
            if is_reorg and db_tx and db_tx['hash'] == transaction['hash'] and db_tx['status'] == 'confirmed':
                if transaction['blockNumber'] is None:
                    log.error("Unexpectedly got unconfirmed transaction again after reorg. hash: {}".format(db_tx['hash']))
                    # this shouldn't really happen. going to log and abort
                    return db_tx['transaction_id']
                new_blocknumber = parse_int(transaction['blockNumber'])
                if new_blocknumber != db_tx['blocknumber']:
                    async with self.pool.acquire() as con:
                        await con.execute(
                            "UPDATE transactions SET blocknumber = $1 "
                            "WHERE transaction_id = $2",
                            new_blocknumber, db_tx['transaction_id'])
                return db_tx['transaction_id']

            # check for erc20 transfers
            erc20_transfers = []
            if transaction['blockNumber'] is not None and \
               'logs' in transaction and \
               len(transaction['logs']) > 0:

                # find any logs with erc20 token related topics
                for _log in transaction['logs']:
                    if len(_log['topics']) > 0:
                        # Transfer(address,address,uint256)
                        if _log['topics'][0] == TRANSFER_TOPIC:
                            # make sure the log address is for one we're interested in
                            is_known_token = await con.fetchval("SELECT 1 FROM tokens WHERE contract_address = $1", _log['address'])
                            if not is_known_token:
                                continue
                            if len(_log['topics']) == 3 and len(_log['data']) == 66:
                                # standard erc20 structure
                                erc20_from_address = decode_single(('address', '', []), data_decoder(_log['topics'][1]))
                                erc20_to_address = decode_single(('address', '', []), data_decoder(_log['topics'][2]))
                                erc20_value = decode_abi(['uint256'], data_decoder(_log['data']))[0]
                            elif len(_log['topics']) == 1 and len(_log['data']) == 194:
                                # non-indexed style Transfer events
                                erc20_from_address, erc20_to_address, erc20_value = decode_abi(
                                    ['address', 'address', 'uint256'], data_decoder(_log['data']))
                            else:
                                log.warning('Got invalid erc20 Transfer event in tx: {}'.format(transaction['hash']))
                                continue
                            erc20_is_interesting = await con.fetchval(
                                "SELECT 1 FROM token_registrations "
                                "WHERE eth_address = $1 OR eth_address = $2",
                                erc20_from_address, erc20_to_address)
                            if erc20_is_interesting:
                                erc20_transfers.append((_log['address'], get_transaction_log_index(_log), erc20_from_address, erc20_to_address, hex(erc20_value), 'confirmed'))

                        # special checks for WETH, since it's rarely 'Transfer'ed, but we
                        # still need to update it
                        elif (_log['topics'][0] == DEPOSIT_TOPIC or _log['topics'][0] == WITHDRAWAL_TOPIC) and _log['address'] == WETH_CONTRACT_ADDRESS:
                            eth_address = decode_single(('address', '', []), data_decoder(_log['topics'][1]))
                            erc20_is_interesting = await con.fetchval(
                                "SELECT 1 FROM token_registrations "
                                "WHERE eth_address = $1",
                                eth_address)
                            if erc20_is_interesting:
                                erc20_value = decode_abi(['uint256'], data_decoder(_log['data']))[0]
                                if _log['topics'][0] == DEPOSIT_TOPIC:
                                    erc20_to_address = eth_address
                                    erc20_from_address = "0x0000000000000000000000000000000000000000"
                                else:
                                    erc20_to_address = "0x0000000000000000000000000000000000000000"
                                    erc20_from_address = eth_address
                                erc20_transfers.append((WETH_CONTRACT_ADDRESS, get_transaction_log_index(_log), erc20_from_address, erc20_to_address, hex(erc20_value), 'confirmed'))

            elif transaction['blockNumber'] is None and db_tx is None:
                # transaction is pending, attempt to guess if this is a token
                # transaction based off it's input
                if transaction['input']:
                    data = transaction['input']
                    if (data.startswith("0xa9059cbb") and len(data) == 138) or (data.startswith("0x23b872dd") and len(data) == 202):
                        token_value = hex(int(data[-64:], 16))
                        if data.startswith("0x23b872dd"):
                            erc20_from_address = "0x" + data[34:74]
                            erc20_to_address = "0x" + data[98:138]
                        else:
                            erc20_from_address = from_address
                            erc20_to_address = "0x" + data[34:74]
                        erc20_transfers.append((to_address, 0, erc20_from_address, erc20_to_address, token_value, 'unconfirmed'))
                    # special WETH handling
                    elif data == '0xd0e30db0' and transaction['to'] == WETH_CONTRACT_ADDRESS:
                        erc20_transfers.append((WETH_CONTRACT_ADDRESS, 0, "0x0000000000000000000000000000000000000000", transaction['from'], transaction['value'], 'unconfirmed'))
                    elif data.startswith('0x2e1a7d4d') and len(data) == 74:
                        token_value = hex(int(data[-64:], 16))
                        erc20_transfers.append((WETH_CONTRACT_ADDRESS, 0, transaction['from'], "0x0000000000000000000000000000000000000000", token_value, 'unconfirmed'))

            if db_tx:
                is_interesting = True
            else:
                # find out if there is anyone interested in this transaction
                is_interesting = await con.fetchval("SELECT 1 FROM notification_registrations "
                                                    "WHERE eth_address = $1 OR eth_address = $2",
                                                    to_address, from_address)
            if not is_interesting and len(erc20_transfers) > 0:
                for _, _, erc20_from_address, erc20_to_address, _, _ in erc20_transfers:
                    is_interesting = await con.fetchval("SELECT 1 FROM notification_registrations "
                                                        "WHERE eth_address = $1 OR eth_address = $2",
                                                        erc20_to_address, erc20_from_address)
                    if is_interesting:
                        break
                    is_interesting = await con.fetchval("SELECT 1 FROM token_registrations "
                                                        "WHERE eth_address = $1 OR eth_address = $2",
                                                        erc20_to_address, erc20_from_address)
                    if is_interesting:
                        break

            if not is_interesting:
                return

            if db_tx is None:
                # if so, add it to the database and trigger an update
                # add tx to database
                db_tx = await con.fetchrow(
                    "INSERT INTO transactions "
                    "(hash, from_address, to_address, nonce, "
                    "value, gas, gas_price, "
                    "data) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "RETURNING transaction_id",
                    transaction['hash'], from_address, to_address, parse_int(transaction['nonce']),
                    hex(parse_int(transaction['value'])), hex(parse_int(transaction['gas'])), hex(parse_int(transaction['gasPrice'])),
                    transaction['input'])

            for erc20_contract_address, transaction_log_index, erc20_from_address, erc20_to_address, erc20_value, erc20_status in erc20_transfers:
                is_interesting = await con.fetchval("SELECT 1 FROM notification_registrations "
                                                    "WHERE eth_address = $1 OR eth_address = $2",
                                                    erc20_to_address, erc20_from_address)
                if not is_interesting:
                    is_interesting = await con.fetchrow("SELECT 1 FROM token_registrations "
                                                        "WHERE eth_address = $1 OR eth_address = $2",
                                                        erc20_to_address, erc20_from_address)

                if is_interesting:
                    await con.execute(
                        "INSERT INTO token_transactions "
                        "(transaction_id, transaction_log_index, contract_address, from_address, to_address, value, status) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                        "ON CONFLICT (transaction_id, transaction_log_index) DO UPDATE "
                        "SET from_address = EXCLUDED.from_address, to_address = EXCLUDED.to_address, value = EXCLUDED.value",
                        db_tx['transaction_id'], transaction_log_index, erc20_contract_address,
                        erc20_from_address, erc20_to_address, erc20_value, erc20_status)

            manager_dispatcher.update_transaction(
                db_tx['transaction_id'],
                'confirmed' if transaction['blockNumber'] is not None else 'unconfirmed')
            return db_tx['transaction_id']

    @log_unhandled_exceptions(logger=log)
    async def handle_reorg(self):
        log.info("REORG encounterd at block #{}".format(self.last_block_number))
        blocknumber = self.last_block_number
        forked_at_blocknumber = None
        BLOCKS_PER_ITERATION = 10
        while True:
            bulk = self.eth.bulk()
            for i in range(BLOCKS_PER_ITERATION):
                if blocknumber - i >= 0:
                    bulk.eth_getBlockByNumber(blocknumber - i, with_transactions=True)
            node_results = await bulk.execute()
            async with self.pool.acquire() as con:
                db_results = await con.fetch("SELECT * FROM blocks WHERE blocknumber <= $1 ORDER BY blocknumber DESC LIMIT $2",
                                             blocknumber, BLOCKS_PER_ITERATION)
            while node_results:
                node_block = node_results[0]
                db_block = None
                while db_results:
                    db_block = db_results[0]
                    if parse_int(node_block['number']) != db_block['blocknumber']:
                        log.error("Got out of order blocks when handling reorg: expected: {}, got: {}".format(
                            parse_int(node_block['number']), db_block['blocknumber']))
                        db_results = db_results[1:]
                    else:
                        break
                if db_block is None:
                    # we don't know about any more blocks, so we can just reorg the whole thing!
                    break

                if node_block['hash'] == db_block['hash']:
                    log.info("FORK found at block #{}".format(db_block['blocknumber']))
                    forked_at_blocknumber = db_block['blocknumber']
                    break

                log.info("Mismatched block #{}. old: {}, new: {}".format(
                    db_block['blocknumber'], db_block['hash'], node_block['hash']))

                node_results = node_results[1:]
                db_results = db_results[1:]

            if forked_at_blocknumber is not None:
                break

            blocknumber = blocknumber - BLOCKS_PER_ITERATION
            # if the blocknumber goes too low, abort finding the reorg
            if blocknumber <= 0 or blocknumber < self.last_block_number - 1000:
                log.error("UNABLE TO FIND FORK POINT FOR REORG")
                return False

        if forked_at_blocknumber is None:
            log.error("Error: unexpectedly broke from reorg point finding loop")
            return False

        async with self.pool.acquire() as con:
            # mark blocks as stale
            await con.execute("UPDATE blocks SET stale = TRUE WHERE blocknumber > $1",
                              forked_at_blocknumber)
            # revert collectible's last block numbers
            await con.execute("UPDATE collectibles SET last_block = $1 WHERE last_block > $1",
                              forked_at_blocknumber - 1)

        self.last_block_number = forked_at_blocknumber
        return True

    def run_sanity_check(self):
        self._sanity_check_process = asyncio.get_event_loop().create_task(self.sanity_check())

    @log_unhandled_exceptions(logger=log)
    async def sanity_check(self):
        if self._shutdown:
            return
        # check that filter ids are set to something
        if self._new_pending_transaction_filter_id is None:
            await self.register_new_pending_transaction_filter()
        # check that poll callback is set and not in the past
        if self._poll_schedule is None:
            log.warning("Filter poll schedule is None!")
            self.schedule_filter_poll()
        elif self._filter_poll_process is not None:
            pass
        else:
            if self._poll_schedule._when < self._poll_schedule._loop.time():
                log.warning("Filter poll schedule is in the past!")
                self.schedule_filter_poll()
        # make sure there was a block somewhat recently
        ok = True
        time_since_last_new_block = int(asyncio.get_event_loop().time() - self._last_saw_new_block)
        if time_since_last_new_block > NEW_BLOCK_TIMEOUT:
            log.warning("Haven't seen any new blocks for {} seconds".format(time_since_last_new_block))
            ok = False
        self._sanity_check_schedule = asyncio.get_event_loop().call_later(SANITY_CHECK_CALLBACK_TIME, self.run_sanity_check)
        if ok:
            await self.redis.setex("monitor_sanity_check_ok", SANITY_CHECK_CALLBACK_TIME * 2, "OK")
        self._sanity_check_process = None

    @property
    def redis(self):
        return get_redis_connection()

    async def shutdown(self):

        self._shutdown = True

        try:
            await self.filter_eth.close()
        except:
            pass

        if self._check_schedule:
            self._check_schedule.cancel()
        if self._poll_schedule:
            self._poll_schedule.cancel()
        if self._sanity_check_schedule:
            self._sanity_check_schedule.cancel()

        # let the current iteration of each process finish if running
        if self._block_checking_process:
            await self._block_checking_process
        if self._filter_poll_process:
            await self._filter_poll_process
        if self._sanity_check_process:
            await self._sanity_check_process
        if self._process_unconfirmed_transactions_process:
            await self._process_unconfirmed_transactions_process

        self._startup_future = None


if __name__ == '__main__':
    from toshieth.app import extra_service_config
    extra_service_config()
    monitor = BlockMonitor()
    monitor.start()
    asyncio.get_event_loop().run_forever()
