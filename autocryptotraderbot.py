# bot to trade crypto based on messages received in Telegram channels

from importscript import *

date= datetime.now().strftime("%Y %m %d %I%M").split(" ")
filename= f"mybot_{date[0]}_{date[1]}_{date[2]}.log" 
logger = logging.getLogger(__name__)
logging.basicConfig(filename= filename, level=logging.INFO, 
                            filemode= "w",
                            format="%(asctime)s - %(levelname)s - %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

api_id= os.environ["TELEGRAM_API_ID"]
api_hash= os.environ["TELEGRAM_API_HASH"]
bot_token= os.environ["TELEGRAM_BOT_TOKEN"]
#account_sid= os.environ["TWILIO_ACCOUNT_SID"]
#auth_token= os.environ["TWILIO_AUTH_TOKEN"]
jup_api_key= os.environ["JUP_API_KEY"]
mob_api_key= os.environ["MOB_API_KEY"]
user_locks, user_sessions, user_clients, active_tasks= {}, {}, {}, {} # since we're using stringsessions these don't need to be persistent dbs
slippage_dict= {}
wallet_dict= {}
session_name_dict= {}

# for listener
current_task= {}

http_session= None
bot= TelegramClient("bot", api_id, api_hash)

@contextmanager
def db_conn(path):
    """Context manager to easily connect to sqlite3 databases."""
    conn= sql.connect(path)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.critical(f"SQL error: {e}")
    finally:
        conn.close()

with db_conn("userinfo.db") as db_1, db_conn("translog.db") as db_2: 
    db_1.execute("CREATE TABLE IF NOT EXISTS info(userID, hash, salt_auth, salt_enc, enc_key, enc_data, is_str)")
    db_2.execute("CREATE TABLE IF NOT EXISTS info(userID, token_wallet, session)")

@bot.on(events.NewMessage(pattern=r'^/')) # to interrupt telegram functions when a user sends another function call
async def fork(event):
    id= event.sender_id
    functions= {**{"/"+ key: key for key, value in globals().items() if (callable(value) and key!= "main" and value.__module__== __name__)}, 
                                                                                                    "/2FA": "twoFA"} # prevent user code injection
    if id in active_tasks:
        task= active_tasks[id]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    
    if event.text in functions:
        active_tasks[id]= asyncio.create_task(eval(functions[event.text]+ "(event)"))

async def call_wrap(url, method, retries= 15, **kwargs): # change to aiohttp at some point b/c highly async 
    """Wrapper for API calls."""
    for r in range(1, retries+1):
        try:
            response= await http_session.request(method, url, raise_for_status= True, **kwargs)  
            logger.info(f"The {url} API call was successful! Params: {kwargs}") 
            return response
        except aiohttp.ClientResponseError as e:
                code= e.status
                if 400 <= code <= 499:
                    if code== 429:
                        logger.error(f"API calls have been rate-limited. Retry {r}", exc_info= True)
                        await asyncio.sleep(r)
                        continue
                    logger.error(f"Client side error: {e}", exc_info= True)
                    break
                else: 
                    logger.error(f"Internal server error: {e}. Retry {r}", exc_info= True)
                    await asyncio.sleep(r) # might not need this
                    continue
        except aiohttp.ClientError as e:
            logger.critical(f"Request failed: {e}")
            break
        except Exception as e:
            logger.critical(f"An error unrelated to the request caused an unexpected failure: {e}")
            break

    return

async def find_price(num_tokens, mint):
    """Find the price of a token in USD."""
    token_base_url= "https://api.jup.ag/tokens/v2/"
    #price_response= requests.get(token_base_url+ "search", headers= {"x-api-key": jup_api_key}, params= {"query": mint}).json()

    price_response= await call_wrap(token_base_url+ "search", "get", headers= {"x-api-key": jup_api_key}, params= {"query": mint}).json()

    return num_tokens* price_response[0]["usdPrice"]

async def decimals(mint):
    """Find out how many decimals are in the base token."""
    sol_url= "https://api.mainnet.solana.com"
    
    sol_response= await call_wrap(sol_url, "post", headers= {"Content-type": "application/json"}, json= {"jsonrpc": "2.0",
                                                                                                "id": 1,
                                                                                                "method": "getTokenSupply",
                                                                                                "params": [mint]}).json()
    
    logger.info(sol_response)
    return float("1"+ "".join(["0" for _ in range(sol_response["result"]["value"]["decimals"])]))

async def transaction(input_mint, output_mint, num, slippage, **kwargs):
    """Execute a token transaction."""
    base_url= "https://api.jup.ag/swap/v2/"
    x= SimpleNamespace(**kwargs)
    
    order_response= await call_wrap(base_url+ "order", "get", headers= {"x-api-key": jup_api_key}, params= {"inputMint": input_mint,
                                                                               "outputMint": output_mint,
                                                                               "taker": x.my_pub_key,
                                                                               "amount": num, # amount of sol to use to buy the token
                                                                               **({"slippageBps": slippage} if slippage is not None else {})}).json()
    swap_instruction= order_response["transaction"]
    requestId= order_response["requestId"]
    lastValidBlockHeight= order_response["lastValidBlockHeight"]

    raw_tx= VersionedTransaction.from_bytes(base64.b64decode(swap_instruction))
    signed_tx= VersionedTransaction(raw_tx.message, [x.wallet])
    encoded_tx= base64.b64encode(bytes(signed_tx)).decode()
    
    execute_response= await call_wrap(base_url+ "execute", "post", headers= {"x-api-key": jup_api_key}, json= {"signedTransaction": encoded_tx, 
                                                                                                    "requestId": requestId,
                                                                                                    "lastValidBlockHeight": lastValidBlockHeight}).json()
    
    if execute_response["status"].lower()== "success":
        return execute_response
    else:
        error_str, code= execute_response["error"], execute_response["code"]
        logger.error(f"Failed transaction! Error code {code} with reason: {error_str}", exc_info= True)
    
    return

async def order_flow(stop_event, wait= 300, **kwargs):
    """The current transaction flow for all interpreters."""
    x= SimpleNamespace(**kwargs)

    try:
        lamport, token_scale= await decimals(x.sol_mint), await decimals(x.token_mint)

        bought= await transaction(x.sol_mint, x.token_mint, str(ceil(x.num* lamport)), x.slippage, my_pub_key= x.pubkey, wallet= x.wallet)

        spent_sol= float(bought["inputAmountResult"])
        got_token= float(bought["outputAmountResult"])
        initial_usd_token= await find_price(got_token/ token_scale, x.token_mint)
        total_time= time.time()+ wait
        interval= 1.2 # internal param

        logger.info(f"Congrats! The transaction was a success!!! Executed at {spent_sol/ lamport} SOL or ${initial_usd_token}")

        while time.time()< total_time:
            if stop_event.is_set():
                logger.info("Trading has been stopped by the user.")
                break
            usd_token= await find_price(got_token/ token_scale, x.token_mint)
            pc= ((usd_token/ initial_usd_token)- 1)*100
            if not -x.sl<= pc<= x.tp:
                logger.info(f'''The price of the token with address {x.token_mint[:7]}... has gone past your stop loss or take profit. You bought at {initial_usd_token} and 
                            are selling at {usd_token}. This is a percent change of {pc} (pending any slippage or fees).''')
                _ = await transaction(x.token_mint, x.sol_mint, str(int(got_token)), x.slippage, my_pub_key= x.pubkey, wallet= x.wallet) 
                break
            await asyncio.sleep(interval) # to make interruptible
            #else:
            #    logger.info(f"The amount of token with address {x.token_mint[:7]}... recieved is worth ${usd_token} and it has increased {pc}% from when it was bought.")
            #    await asyncio.sleep(interval)
        return
    
    except asyncio.CancelledError:
        try:
            logger.info(f"Trade has been cancelled by the user. Initiating sale of {x.token_mint}...")
            _= await transaction(x.sol_mint, x.token_mint, str(ceil(x.num* lamport)), x.slippage, my_pub_key= x.pubkey, wallet= x.wallet)
        except Exception as e:
            logger.error(f"Failed to sell token after cancellation: {e}", exc_info= True)
        
    return 


def turn_url_into_qr(url):
    """Creates the QR code for Telegram login."""
    buffer= BytesIO() # store qr code image in memory
    qr= qrcode.QRCode( # exactly what is written in the qrcode docs, use advanced options if I need to customize the QR code later
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4)
    
    qr.add_data(url)
    qr.make(fit= True)
    img= qr.make_image(fill_color= "black", back_color= "white")
    img.save(buffer, format="PNG")
    buffer.name= "qrcode.png" 
    buffer.seek(0)

    return buffer

'''def send_text(phone):
    """Sends a text via SMS gateway to the user's phone."""
    carrier_map= {
        "verizon": "vtext.com",
        "tmobile": "tmomail.net",
        "sprint": "messaging.sprintpcs.com",
        "at&t": "txt.att.net",
        "boost": "smsmyboostmobile.com",
        "cricket": "sms.cricketwireless.net",
        "uscellular": "email.uscc.net",
    }
    carrier= carrier_map["at&t"]
    msg= "What's good, bby girl. This is a test."
    with SMTP("smtp.gmail.com", 587) as smtp:
        smtp.set_debuglevel(2)
        smtp.login(app_username, app_password)
        smtp.sendmail(app_username, f"{phone}@{carrier}", msg)
    
    return'''

def send_text(phone):
    """Sends a text via SMS gateway to the user's phone."""
    logger.info("This area is under construction.")

    '''texter= Client(account_sid, auth_token)
    message= texter.messages.create(
        body= "What's good, bby girl. This is a test.",
        from_= "+19545933133",
        to= phone
    )
    
    return'''

    return

class Interpreter:
    """Interprets messages received from a channel and takes an action."""
    def __init__(self, **kwargs):
        self.sol= "So11111111111111111111111111111111111111112"
        for key, val in kwargs.items():
            setattr(self, key, val)
    
    async def first_interpreter(self, msg= None):
        logger.info("I'm interpreting!")
        try:
            if msg:
                for line in msg.splitlines():
                    match= re.search(r"jup\.ag/swap/SOL-([A-Za-z0-9]+)", line, re.IGNORECASE)
                    if match:
                        token= match.group(1)
                        logger.info(f"I will buy the token with address {token}.")
                        await order_flow(self.sol, token, self.slippage, self.amount, self.tp, self.sl, self.pubkey, self.wallet, self.wait_time)
        except (NameError, AttributeError):
            logger.critical("No message was passed to the interpreter!")
        
        with db_conn("translog.db") as db:
            db.execute("INSERT OR IGNORE INTO info(token_wallet) VALUES (?)", (token,))

        return

async def create_listener(id, num_messages, **kwargs): # the "event.sender_id" for create_listener is from the group being listened to, NOT the user like in /trade or other commands. So we must pass on from /trade
    """Create listener using an event handler to process messages one at a time."""
    logger.info("I've started.")
    stop_event= asyncio.Event()

    if num_messages== 0:
            await bot.send_message(id, "You specified 0 messages to read, so the listener will not start. Please run '/trade' again.")
            return

    args= SimpleNamespace(**kwargs) # this doesn't help simplify much but it's cool
    read= 0

    async def inserter(event):
        task= current_task.get(id)

        if task and not task.done():
            logger.info("Waiting for trade to complete before looking at another message.")
            return
        
        current_task[id]= asyncio.create_task(listener(event))

    async def listener(event): 
        logger.info("I'm listening.")
        nonlocal read

        chosen_method= getattr(Interpreter(slippage= args.slippage, 
                                           tp= args.tp, 
                                           sl= args.sl, 
                                           amount= args.amount, 
                                           wait_time= args.wait_time,
                                           pubkey= args.pubkey,
                                           wallet= args.wallet), args.choice)
        await chosen_method(msg= event.text)

        read+= 1
        if num_messages is not None and read>= num_messages:
            stop_event.set()

        return

    args.client.add_event_handler(inserter, events.NewMessage(chats= args.group))

    if args.num_text.endswith("m"):
        await asyncio.sleep(args.num_text[:-1]*60)
    elif args.num_text.endswith("h"):
        await asyncio.sleep(args.num_text[:-1]*3600)
    else:
        await stop_event.wait()
    
    args.client.remove_event_handler(inserter, events.NewMessage(chats= args.group))
    
    '''task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass'''

    return

async def keypair_gen(user_input, chat_id):
    with bot.conversation(chat_id) as conv:
        try:
            keypair= Keypair.from_base58_string(user_input.decode()) if isinstance(user_input, bytes) else None
            if not keypair:
                if getattr(user_input, "document", None):
                    if user_input.document.mime_type== "application/json": # .json methods
                        json_file= await user_input.download_media()
                        with open(json_file, "r") as f:
                            secret= json.load(f)
                            try:
                                keypair= Keypair.from_bytes(secret)
                            except Exception as e:
                                logger.error(f"Keypair ran into a problem: {e}")
                                await conv.send_message("Unable to verify a keypair. Please correct your .json.")
                    else:
                        await conv.send_message("It seems like you didn't attach a .json file. Please try again.")
                    os.remove(user_input.document.attributes[-1].file_name)
                elif getattr(user_input, "text", None):
                    splitted= user_input.split(" ")
                    if len(splitted)> 1 and re.search(r"/", splitted[-1]): # seed + derivation path
                        seed, derivation= " ".join(splitted[:-1]), splitted[-1]
                        keypair= Keypair.from_seed_and_derivation_path(seed, derivation)
                    elif len(splitted)> 1: # seed phrase method
                        try: 
                            mnemo= Mnemonic("english")
                            seed= mnemo.to_seed(user_input)
                            keypair= Keypair.from_seed(seed[:32])
                        except Exception as e:
                            logger.error(f"Keypair ran into a problem: {e}", exc_info= True)
                            await conv.send_message("Unable to verify a keypair. Please correct your seed phrase.")
                    else: # base58
                        try:
                            keypair= Keypair.from_base58_string(user_input)
                        except Exception as e:
                            logger.error(f"Keypair ran into a problem: {e}")
                            await conv.send_message("I've detected that you are trying to provide a base58 string. I cannot verify a keypair. Please correct your string or use a different method.")
        except Exception as e:
            await conv.send_message("An unexpected error happened. Please try again.")
            logger.critical(f"Unexpected error: {e}")

    return keypair

def delete_user_info(evnt): # lazy function
    with db_conn("userinfo.db") as db:
        db.execute("DELETE FROM info WHERE userID= ?", (evnt.sender_id,))
    
    return  

def create_key(password, salt, context= b""):
    kdf= Scrypt(salt= salt+ context, length= 32, n= 2**14, r= 8, p= 1)

    return base64.urlsafe_b64encode(kdf.derive(password.encode()))

def my_encrypt(password, data):
    salt_auth, salt_acc= os.urandom(16), os.urandom(16)
    pass_hash, user_key= create_key(password, salt_auth, b"auth"), create_key(password, salt_acc, b"acc")
    gen_key= Fernet.generate_key()
    f_data= Fernet(gen_key)
    is_text= "True" if isinstance(data, str) else "False" # for compatability with later conditionals
    if is_text== "True":
        protected= f_data.encrypt(data.encode()) # get rid of the is_text stuff; unneeded now
    else:
        protected= f_data.encrypt(data)
    f_key= Fernet(user_key)
    enc_key= f_key.encrypt(gen_key)

    return pass_hash, salt_auth, salt_acc, enc_key, protected, is_text

def my_decrypt(password, pass_hash, salt_auth, salt_acc, enc_key, protected_data, text_check):
    attempt= create_key(password, salt_auth, b"auth")
    if not hmac.compare_digest(pass_hash, attempt):
        raise ValueError
    user_key= create_key(password, salt_acc, b"acc")
    f_key= Fernet(user_key)
    dec_key= f_key.decrypt(enc_key)
    f_data= Fernet(dec_key)
    decrypted= f_data.decrypt(protected_data)

    return decrypted

'''@bot.on(events.CallbackQuery)
async def callback(event):
    """To regulate button responses."""
    await event.answer()
    
    # options for 2FA
    if event.data== b"yes_auth_1":
        return
    if event.data== b"no_auth_1":
        return
    if event.data== b"yes_auth_2":
        await event.delete()
        async with bot.conversation(event.sender_id) as conv:
            await conv.send_message("Please enter your phone number without any spaces or dashes. Include your country code. (e.g. +18007132618)")
            while True:
                number= await conv.get_response()
                if not number.text.split("+")[-1].isdigit():
                    await conv.send_message("It seems like you didn't type your number correctly. Check for any mistakes and try again.")
                    continue
                break
            try:
                send_text(number)
                #logger.info(f"Sent message {messID} to {number}")
            except Exception as e:
                logger.error(f"2FA error: {e}")
                await conv.send_message("Something went wrong while trying to send a message to your phone number. Often, this is because your carrier doesn't have an SMS gateway.")
    if event.data== b"no_auth_2":
        await event.delete()
        await bot.send_message(event.sender_id, "Ok. Stay safe and secure!")'''
    

async def login(event): # to prevent SQLite locks
    """Login a user to Telegram."""
    id= event.sender_id # got tired of typing it out

    if id not in user_locks:
        user_locks[id]= asyncio.Lock()
    
    lock= user_locks.setdefault(id, asyncio.Lock())

    async with lock:
        if id in user_clients:
            client= user_clients[id]
        else:
            session_str= user_sessions.get(id)
            client= TelegramClient(
                StringSession(session_str) if session_str else StringSession(),
                api_id,
                api_hash)
            user_clients[id]= client
        
        await client.connect()
        async with bot.conversation(event.chat_id) as conv:
            if not await client.is_user_authorized():
                msg= await conv.send_message("Please choose your login method.", 
                                buttons= [Button.inline("Phone Code", b"phone"), Button.inline("QR Code", b"QR")])
                choice= await conv.wait_event(events.CallbackQuery(func= lambda x: x.sender_id== id and x.message_id== msg.id))
                await msg.delete()

                try:
                    if choice.data== b"phone":
                        # await event.delete()
                        await conv.send_message("What is your phone number? You can write it in several formats including the country code, e.g. +1 (XXX)-XXX-XXXX, +1 XXX-XXX-XXXX, +1 XXX XXX XXXX, +1-XXX-XXX-XXXX, etc.")
                        while True:
                            try:
                                phone= phonenumbers.parse((await conv.get_response()).text)
                            except NumberParseException as e:
                                logger.error(f"Something went wrong while parsing the user's phone number: {e}")
                                await conv.send_message("You wrote your phone number in an unrecognized format. Please try again.")
                                continue
                            if not phonenumbers.is_valid_number(phone): # and phonenumbers.is_possible_number(phone)
                                await conv.send_message("This phone number doesn't exist or isn't registered to a carrier. Please use the phone number attached to your Telegram.")
                                continue
                            formatted= phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)
                            try:
                                login_token= await client.send_code_request(formatted)
                            except errors.FloodWaitError as e:
                                await conv.send_message(
                                    f"Telegram has temporarily limited login code requests. "
                                    f"Please try again in approximately {round(e.seconds/3600, 1)} hours.")
                                logger.error(f"Flood wait: {e.seconds} seconds.")
                                return
                            except errors.PhoneNumberBannedError:
                                await conv.send_message("Your phone number is banned. You will not be able to sign in with it. Please try again.")
                                continue
                            except Exception as e:
                                await conv.send_message("An unknown error occurred. Please try again.")
                                logger.error(f"Telethon couldn't send a code to the user's number: {e}")
                                continue
                            break

                        timeout= f"expire in {login_token.timeout} seconds" if login_token.timeout else "not expire"
                        await conv.send_message(f"A login code was sent to your Telegram. It will {timeout}. Please write it here. You can run '/login' again if you didn't receive it.")
                        while True:
                            code= await conv.get_response()
                            try:
                                await client.sign_in(phone= formatted, code= code.text.strip())
                            except errors.rpcerrorlist.PhoneCodeInvalidError:
                                await conv.send_message("Incorrect code. Try again.")
                                continue
                            except errors.rpcerrorlist.PhoneCodeExpiredError:
                                await conv.send_message("Your code has expired. This may be because it timed out or because you forgot to obfuscate. " \
                                                                                                    "Please run '/login' again. ")
                                return
                            except errors.SessionPasswordNeededError:
                                await conv.send_message("2FA is enabled for this account. Please provide your password.")
                                while True:
                                    password= await conv.get_response()
                                    try:
                                        await client.sign_in(password= password.text)
                                    except errors.PasswordHashInvalidError:
                                        await conv.send_message("Incorrect 2FA password. Please try again.")
                                        continue
                                    break
                                break
                    
                    if choice.data== b"QR":
                        # await event.delete()
                        try:
                            qr_login= await client.qr_login()
                            qr_img= turn_url_into_qr(qr_login.url)
                            await conv.send_file(qr_img, caption="Please scan the QR code with Telegram to login. This QR code is valid for ~30 seconds. You may need to submit '/login' again if it expires.")
                            await qr_login.wait()
                            qr_img.close()
                        except asyncio.TimeoutError as e:
                            await conv.send_message("Sorry, but the QR code has expired. Please submit '/login' again.")
                            return
                        except errors.SessionPasswordNeededError:
                            await conv.send_message("2FA is enabled for this account. Please provide your password.")
                            while True:
                                password= await conv.get_response()
                                try:
                                    await client.sign_in(password= password.text)
                                except errors.PasswordHashInvalidError:
                                    await conv.send_message("Incorrect 2FA password. Please try again.")
                                    continue
                                break
                except Exception as e:
                    logger.critical(f"Error occurred in '/login': {e}")
                    await conv.send_message("Sorry, something went wrong during the login process. Please try submit '/login' again.")

        if await client.is_user_authorized():
            me= await client.get_me()
            await bot.send_message(id, f"Successfully signed in to Telegram as {me.username}.")
            user_sessions[id]= client.session.save()

    return

async def start(event):
    """Sends a welcome message to the user when they start the bot."""
    await bot.send_message(event.sender_id, "Hello! I am a crypto trading bot that is currently in development.") 

# bring this back at some point
'''async def twoFA(event):
    """Allows user to enable 2FA."""
    if auth_flag:
        await bot.send_message(event.sender_id, "It seems you have already enabled 2FA. Would you like to disable it?",
                               buttons=[Button.inline('Yes', b'yes_auth_1'), Button.inline('No', b'no_auth_1')])
    else:
        await bot.send_message(event.sender_id, "Would you like to enable 2FA?",
                               buttons=[Button.inline('Yes', b'yes_auth_2'), Button.inline('No', b'no_auth_2')])'''

async def stats(event):
    """Display user PnL and win rate."""
    base_url= "https://api.mobula.io/api/"
    id= event.sender_id

    wallet= wallet_dict.get(id)
    if not wallet:
        await bot.send_message(id, "It looks like you haven't ran the bot yet. Start trading and look at your wins (or losses)!")
        return
    
    with db_conn("translog.db") as db:
        sesh_wall= [tok for tok, session in db.execute("SELECT token_wallet, session FROM info WHERE userID= ?", 
                                                       (id,)) if session== session_name_dict.get(id)]

    data= requests.get(base_url+ "2/wallet/positions", headers= {"Authorization": mob_api_key}, params= {"wallet": wallet,
                                                                                             "blockchains": "solana"}).json()["data"]
    # test; delete when complete
    one_address= next(trade["token"]["address"] for trade in data)
    logger.info(f"Here is a token address: {one_address}")

    # total PnL and Win rate
    tot_pnl= [trade["realizedPnlUSD"]- trade["totalFeesPaidUSD"] for trade in data]
    tot_win= (sum(1 for pnl in tot_pnl if pnl> 0)/ len(tot_pnl))* 100 if len(tot_pnl)> 0 else 0
    
    message_1= f"\033[1mOVERALL STATS:\033[0m\nPnL: {sum(tot_pnl)}\nWin Rate: {tot_win}"
    message_2= "\033[1mSESSION STATS:\033[0m\nN/A"

    if sesh_wall:
        sesh_pnl= [trade["realizedPnlUSD"]- trade["totalFeesPaidUSD"] for trade in data 
                                        if trade["token"]["address"] in sesh_wall]
        sesh_win= (sum(1 for pnl in sesh_pnl if pnl> 0)/ len(sesh_pnl))* 100 if len(sesh_pnl)> 0 else 0
        message_2= f"\033[1mSESSION STATS:\033[0m\nPnL: {sum(sesh_pnl)}\nWin Rate: {sesh_win}"
    
    await bot.send_message(id, message_1+ message_2)
    return 

async def wipe(event):
    """Allows a user to erase all stored personal data."""
    id= event.sender_id
    
    async with bot.conversation(event.chat_id) as conv:
        msg= await conv.send_message(id, "This option will erase any personal data that has been stored. You will be prompted to provide another password and reenter your wallet details when you run '/trade'. Do you want to continue?",
                          buttons=[Button.inline('Yes', b'yes'), Button.inline('No', b'no')])
        choice= await conv.wait_event(events.CallbackQuery(func= lambda x: x.sender_id== id and x.message_id== msg.id))
        await msg.delete()
    
        # options for wipe
        if choice.data== b"yes":
            with db_conn("userinfo.db") as db:
                user_id= db.execute("SELECT 1 FROM info WHERE userID= ?", (event.sender_id,)).fetchone()
                if not user_id:
                    await conv.send_message("Your info could not be found! You should be able to run '/trade' and specify a password.")
                    return
            try:
                delete_user_info(event)
                await conv.send_message("Your info has been successfully deleted! You can now run '/trade' and specify a new password.")
            except Exception:
                await conv.send_message("For some reason, your info could not be deleted. Please try to rerun '/trade' or contact the owner of this bot.")
                return
            
        if choice.data== b"no":
            await bot.send_message(event.sender_id, "Ok, no problemo!")

    return

async def trade(event):
    """Main trading logic."""
    logger.info("Performing actions from command '/trade'...")
    id= event.sender_id
    
    client= user_clients.get(id)
    
    async with bot.conversation(id) as conv:
        if not client:
            await conv.send_message("Please login to your Telegram account using '/login' before running this command.")
            return
        
        with db_conn("userinfo.db") as db_1, db_conn("translog.db") as db_2:
            row_pswd= db_1.execute("SELECT hash, salt_auth, salt_enc, enc_key, enc_data, is_str FROM info where userID= ?", (id,)).fetchone()
            user_hash, user_salt_auth, user_salt_enc, user_enc_key, user_enc_data, check= row_pswd or [None for _ in range(6)]
        
            sess_name= "".join([choice(ascii_letters+ digits) for _ in range(15)]) # low collision prob (birthday problem)
            session_name_dict[id]= sess_name
            db_2.execute("INSERT OR IGNORE INTO info(userID, session) VALUES (?, ?)", (id, sess_name))

        if not any([user_hash, user_salt_auth, user_salt_enc, user_enc_key, user_enc_data, check]):
            await conv.send_message("I need your info to send transactions! First, please specify a *strong* password to use when accessing your wallet in the future.")
            user_password= await conv.get_response()
            await conv.send_message("You have four options to provide your wallet details: \n\n1. Attach a keypair .json \n2. Provide the secret key in base58 format \n3. Provide your seed phrase \n4. Provide your seed phrase and derivation path (e.g. *your seed phrase* m/44'/501'/0'/0')")
            while True:
                info_mes= await conv.get_response(timeout= 300)
                try:
                    keypair=  await keypair_gen(info_mes, event.chat_id)
                except Exception:
                    continue
                break 
            my_hash, my_salt_auth, my_salt_acc, my_enc_key, enc_data, my_check= my_encrypt(user_password.text, str(keypair))
            with db_conn("userinfo.db") as db:
                db.execute(f"INSERT OR IGNORE INTO info VALUES (?, ?, ?, ?, ?, ?, ?)", (id, my_hash, my_salt_auth, my_salt_acc, my_enc_key, enc_data, my_check))

        elif all([user_hash, user_salt_auth, user_salt_enc, user_enc_key, user_enc_data, check]):
            await conv.send_message("Please enter your password.")
            while True:
                attempt= await conv.get_response()
                try:
                    secret= my_decrypt(attempt.text, user_hash, user_salt_auth, user_salt_enc, user_enc_key, user_enc_data, check)
                except Exception as e:
                    logger.critical(e)
                    await conv.send_message("Incorrect password. Please try again or reset your password using the command '/wipe'.")
                    continue
                break
            try:
                keypair= await keypair_gen(secret, event.chat_id)
            except Exception:
                await conv.send_message("Sorry, your details couldn't be used to generate a keypair. This may be due to corruption or some other reason. Please rerun '/trade'. You will be prompted to create another password. It can be the same as the last one, but this isn't recommended.")
                delete_user_info(event) 
                return
            
        else:
            await conv.send_message("Sorry, an error occurred during password encryption or user data retrieval. Please rerun '/trade'. For security reasons, you will be prompted to create another password. It can be the same as the last one, but this isn't recommended.")
            delete_user_info(event)
            return
        
        pubkey= keypair.pubkey()

        wallet_dict[id]= pubkey

        await conv.send_message(f"Connected to wallet with public key: {pubkey}") 
        channels, channel_dict= "", {}

        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                channels+= f"ID: {dialog.id} | Name: {dialog.name}\n"
                channel_dict[str(dialog.id)]= dialog.name
                channel_dict[dialog.name.lower()] = dialog.name

        await conv.send_message(f"Please choose a Telegram channel (either ID or name) to monitor:\n\n {channels}")
        while True:
            chan_res= await conv.get_response()
            chan_text= chan_res.text.strip().lower()
            if chan_text in channel_dict:
                group= channel_dict[chan_text]

                await conv.send_message(f"Great! I will monitor {group}. How many messages would you like me to read before stopping the listener? You can also specify a time limit in the format '5m' for 5 minutes or '1h' for 1 hour.") 
                while True:
                    num_res= await conv.get_response()
                    num_text= num_res.text.strip().lower()

                    if re.fullmatch(r"\d+[mh]?", num_text):
                        if num_text.endswith(("m", "h")):
                            num_mess= None
                            await conv.send_message(f"Sounds good! I will read messages for {num_text}.")
                        else:
                            num_mess= int(num_text)
                            await conv.send_message(f"Sounds good! I will read {num_mess} message(s).")
                    else:
                        await conv.send_message("Invalid input. Please enter a valid number.")
                        continue
                    break
                break
            else:
                await conv.send_message("Sorry, I didn't recognize that channel. Please try again.")

        methods, method_list="", [att for att in Interpreter.__dict__ if callable(getattr(Interpreter(), att))
                                  and not att.startswith("__")] 
        for num, att in enumerate(method_list, 1):
                methods+= f"{num}. {att}\n"
        await conv.send_message(f"Please choose an interpreter:\n\n{methods}")

        while True:
            intr_res= await conv.get_response()
            intr_text= intr_res.text.strip().lower() # this is fine, all interpreters will be in lowercase
            if intr_text in method_list:
                await conv.send_message(f"Ok! I will use this interpreter: {intr_text}.")
            else:
                await conv.send_message("Sorry, I don't recognize that interpreter. Please try again. Make sure to include any special characters, like _ , *, &, %, etc.")
                continue
            break
        await conv.send_message("Please specify your take profit and stop loss in percent and separated by a space (e.g. 20 25 would be a take profit at 20% and a stop loss at 25%).")

        while True:
            bounds= await conv.get_response()
            try:
                upper, lower= [abs(float(x)) for x in bounds.text.split(" ")]
                if lower>= 100:
                    conv.send_message("Your stop loss cannot exceed 99.9%. Please try again.")
                    continue
            except ValueError:
                await conv.send_message("You submitted your take profit and stop loss in the wrong form. Please try again.")
                continue
            break

        await conv.send_message("How much SOL would you like to trade with?")
        amount= await conv.get_response()
        await conv.send_message("Please set a max duration (in seconds) for which to sell the token after buying. If you'd like to always wait until the stop loss/take profit have been hit, you can type '0'.")
        wait_time= await conv.get_response()

        msg= await conv.send_message("Would you like to set your slippage manually or allow Jupiter to automate it?",
                               buttons=[Button.inline("I'll do it", b"manual"), Button.inline("Let Jupiter handle it", b"auto")]) # have to do a wait_event here
        choice= await conv.wait_event(events.CallbackQuery(func= lambda x: x.sender_id== id and x.message_id== msg.id)) # wait for button logic to be completed
        await msg.delete()

        if choice.data== b"manual":
            await conv.send_message("Please specify your slippage in basis points (100 bp -> 1% difference between the quoted and execution price).")
            while True:
                slippage= await conv.get_response()
                slippage= abs(int(slippage.text))
                if slippage > 10000:
                    await conv.send_message("The slippage cannot be higher than 10000 bp. Please set a lower value.")
                    continue
                break
            slippage_dict[id]= slippage
        
        if choice.data== b"auto":
            await conv.send_message("Good choice! Jupiter typically deals with slippage well.")
    
        await conv.send_message("Let's do some work...")

    try:
        await create_listener(id, num_mess, 
                                    group= group, 
                                    choice= intr_text, 
                                    client= client, 
                                    num_text= num_text,
                                    tp= upper,
                                    sl= lower,
                                    amount= float(amount.text),
                                    wait_time= int(wait_time.text),
                                    slippage= slippage_dict.get(id),
                                    pubkey= pubkey,
                                    wallet= keypair)
    except Exception as e:
        logger.error(f"This didn't work: {e}", exc_info= True)

    logger.info("'/trade' command was successful!")

async def stop(event):
    """Immediately stop all trading activity."""
    id= event.sender_id

    task= current_task.get(id)

    if not task:
        await bot.send_message(id, "There are no trades happening now...")
        return
    
    try:
        task.cancel()
        await bot.send_message(id, "All trades have been successfully shut down.")
    except Exception as e:
        logger.error(f"Something went wrong with /stop: {e}")
    

async def test(event):
    """A simple test to check if the bot is working properly."""
    logger.info("Performing actions from command '/test'...")
    try: 
        async with bot.conversation(event.chat_id) as conv:
            me= await bot.get_me()
            await conv.send_message(f"Hi! This is a test message from {me.username}. Please reply with 'Hi'.")
            while True:
                response= await conv.get_response()
                if response.text.lower()== "hi":
                    await conv.send_message("Thank you for replying! This test was successful.")
                    break
                else:
                    await conv.send_message("Sorry, I didn't understand your response. Please reply with 'Hi' to complete the test.")
    except Exception as e:
        logger.critical(f"Error occurred in '/test': {e}")
    
    logger.info("'/test' command was successful!")

async def main(): # remember not to create separate loops or else errors- keep everything on the Telethon loop
    """Starts the script."""
    global http_session

    http_session= aiohttp.ClientSession()
    try:
        await bot.start(bot_token= bot_token)
    except ConnectionError as e:
        logger.critical(f"{str(e)}, Exiting...")
        raise
    
    logger.info("Starting Bot...")

    try:
        await bot.run_until_disconnected()
    finally:
        await http_session.close()
    
    return

if __name__=="__main__":
    asyncio.run(main())