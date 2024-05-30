import getpass
import logging
import os
from queue import Queue
import re
from threading import Thread

import asyncio
import maigret
from maigret.result import QueryStatus
from maigret.sites import MaigretDatabase
from maigret.report import save_pdf_report, generate_report_context
from telethon.sync import TelegramClient, events

API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')

MAIGRET_DB_FILE = 'data.json'  # wget https://raw.githubusercontent.com/soxoj/maigret/main/maigret/resources/data.json
COOKIES_FILE = "cookies.txt"  # wget https://raw.githubusercontent.com/soxoj/maigret/main/cookies.txt
id_type = "username"
USERNAME_REGEXP = r'^[a-zA-Z0-9-_\.]{5,}$'
ADMIN_USERNAME = '@soxoj'

# top popular sites from the Maigret database
TOP_SITES_COUNT = 1500
# Maigret HTTP requests timeout
TIMEOUT = 30

# Create a queue to hold user requests
request_queue = Queue()


def setup_logger(log_level, name):
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(logging.Formatter('[%(filename)s:%(lineno)d] %(levelname)-3s '
                                               '%(asctime)s %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(log_handler)
    return logger


async def maigret_search(username):
    """
        Main Maigret search function
    """
    logger = setup_logger(logging.WARNING, 'maigret')

    db = MaigretDatabase().load_from_path(MAIGRET_DB_FILE)

    sites = db.ranked_sites_dict(top=TOP_SITES_COUNT)

    results = await maigret.search(username=username,
                                   site_dict=sites,
                                   timeout=TIMEOUT,
                                   logger=logger,
                                   id_type=id_type,
                                   cookies=COOKIES_FILE,
                                   )
    return results


def merge_sites_into_messages(found_sites):
    """
        Join links to found accounts and make telegram messages list
    """
    if not found_sites:
        return ['No accounts found!']

    found_accounts = len(found_sites)
    found_sites_messages = []
    found_sites_entry = found_sites[0]

    for i in range(len(found_sites) - 1):
        found_sites_entry = ', '.join([found_sites_entry, found_sites[i + 1]])

        if len(found_sites_entry) >= 4096:
            found_sites_messages.append(found_sites_entry)
            found_sites_entry = ''

    if found_sites_entry != '':
        found_sites_messages.append(found_sites_entry)

    output_messages = [f'{found_accounts} accounts found:\n{found_sites_messages[0]}'] + found_sites_messages[1:]
    return output_messages


async def search(username):
    """
        Do Maigret search on a chosen username
        :return:
            - list of telegram messages
            - list of dicts with found results data
    """
    try:
        results = await maigret_search(username=username)
    except Exception as e:
        logging.error(e)
        return ['An error occurred, send username once again.'], []

    found_exact_accounts = []
    general_results = [(username, id_type, results)]
    report_context = generate_report_context(general_results)
    save_pdf_report(f"{username}_report.pdf", report_context)

    for site, data in results.items():
        if data['status'].status != QueryStatus.CLAIMED:
            continue
        url = data['url_user']
        account_link = f'[{site}]({url})'

        # filter inaccurate results
        if not data.get('is_similar'):
            found_exact_accounts.append(account_link)

    if not found_exact_accounts:
        return [], []

    messages = merge_sites_into_messages(found_sites=found_exact_accounts)

    # full found results data
    results = list(filter(lambda x: x['status'].status == QueryStatus.CLAIMED, list(results.values())))

    return messages, results


async def handle_event(event, client):
    msg = event.message.message
    bot_logger.info(f'Got a message: {msg}')

    # Handle the /start command
    if msg == '/start':
        sender = await event.get_sender()
        username = sender.first_name if sender.first_name else "there"
        await event.reply(f'Hello, {username}! Welcome to the Maigret bot. '
                          f'Please reply with a target\'s username to begin lookup.')
        return

    # checking for username format
    msg = msg.lstrip('@')
    username_regexp = re.search(USERNAME_REGEXP, msg)

    if not username_regexp:
        bot_logger.warning('Too short username!')
        await event.reply('Username must be more than 4 characters '
                          'and can only consist of Latin letters, '
                          'numbers, minus and underscore.')
        return

    async with client.action(event.chat_id, 'typing'):
        bot_logger.info(f'Started a search by username {msg}.')
        await event.reply(f'Searching by username `{msg}`...')

    # call Maigret
    output_messages, sites = await search(username=msg)
    bot_logger.info(f'Completed: {len(sites)} sites/results and {len(output_messages)} text messages.')

    if not output_messages:
        await event.reply('No accounts found!')
    else:
        for output_message in output_messages:
            try:
                await event.reply(output_message)
                filename = f"{msg}_report.pdf"
                bot_logger.info(f"{filename=}")
                if os.path.isfile(filename):
                    async with client.action(event.from_id, 'document') as action:
                        await client.send_file(event.from_id, filename, progress_callback=action.progress)
                        os.remove(filename)
            except Exception as e:
                bot_logger.error(e, exc_info=True)
                await event.reply('Unexpected error has been occurred. '
                                  f'Write a message to {ADMIN_USERNAME}, he will fix it.')


def process_queue(client, loop):
    asyncio.set_event_loop(loop)
    while True:
        if not request_queue.empty():
            event = request_queue.get()
            bot_logger.info('Processing event from queue')
            future = asyncio.run_coroutine_threadsafe(handle_event(event=event, client=client), loop=loop)
            future.result()  # Wait for the coroutine to finish
            request_queue.task_done()


if __name__ == '__main__':
    logging.basicConfig(
        format='[%(filename)s:%(lineno)d] %(levelname)-3s  %(asctime)s      %(message)s',
        datefmt='%H:%M:%S',
        level=logging.INFO,
    )

    bot_logger = setup_logger(logging.INFO, 'maigret-bot')
    bot_logger.info('I am started.')

    with TelegramClient(getpass.getuser(), API_ID, API_HASH) as tg_client:
        @tg_client.on(events.NewMessage())
        async def handler(event):
            request_queue.put(event)
            # await event.reply('Your request has been added to the queue. Please wait for the response...')

        # Start a background thread to process the queue
        event_loop = asyncio.get_event_loop()
        worker_thread = Thread(target=process_queue, args=(tg_client, event_loop))
        worker_thread.daemon = True
        worker_thread.start()

        tg_client.run_until_disconnected()
