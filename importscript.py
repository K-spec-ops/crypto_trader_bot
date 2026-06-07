# Script including all imports for convenience

import qrcode
import base64 
import asyncio
import requests
import aiohttp
import logging
import sqlite3 as sql
import numpy as np
import boto3
import json
import time
import hmac
import re
import os
from io import BytesIO
from types import SimpleNamespace
from math import ceil, floor
from contextlib import contextmanager
from collections import defaultdict
from twilio.rest import Client
from datetime import datetime
from telethon import TelegramClient, events, errors, Button
from telethon.sessions import StringSession
from mnemonic import Mnemonic 
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from random import choice
from string import ascii_letters, digits
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction