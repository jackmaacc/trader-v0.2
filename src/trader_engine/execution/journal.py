"""Preallocated, checksummed evidence with an exclusive lifetime process lock."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import threading


class JournalError(RuntimeError):
    pass


class JournalLocked(JournalError):
    pass


class JournalCorrupt(JournalError):
    pass


class JournalCapacity(JournalError):
    pass


class ReservedJournal:
    """Each record occupies one reserved slot; a failed append disables future writes.

    A lock protects this evidence file. The executor must additionally ensure that
    different run paths cannot operate the same account concurrently.
    """

    def __init__(self, path, capacity=256, slot_size=4096):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError('Journal capacity must be a positive integer')
        if isinstance(slot_size, bool) or not isinstance(slot_size, int) or slot_size < 256:
            raise ValueError('Journal slots must be at least 256 bytes')
        self.path = Path(path)
        self.capacity = capacity
        self.slot_size = slot_size
        self.usable = True
        self._fd = None
        self._mutex = threading.Lock()
        self._items = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        created = False
        try:
            try:
                self._fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                self._fd = os.open(self.path, flags)
            if not stat.S_ISREG(os.fstat(self._fd).st_mode):
                raise JournalError('Journal evidence must be a regular file')
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalLocked('Another executor holds this journal') from None
            if created:
                # Write actual zero bytes, not sparse truncate: reserve storage now.
                remaining = (capacity + 1) * slot_size
                offset = 0
                zeroes = bytes(min(65536, remaining))
                while remaining:
                    chunk = zeroes[:min(len(zeroes), remaining)]
                    self._write_all(chunk, offset)
                    offset += len(chunk)
                    remaining -= len(chunk)
                self._write_all(self._encode(self._header()), 0)
                os.fsync(self._fd)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            self._items = self._load()
        except BaseException:
            self.usable = False
            self.close()
            raise

    def _header(self):
        return {'magic': 'trader-reserved-journal-v1', 'capacity': self.capacity, 'slot_size': self.slot_size}

    def _encode(self, value):
        if not isinstance(value, dict):
            raise ValueError('Journal records must be JSON objects')
        payload = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(payload) > self.slot_size - 72:
            raise JournalCapacity('Journal record exceeds its reserved slot')
        framed = f'{len(payload):08x}'.encode('ascii') + hashlib.sha256(payload).hexdigest().encode('ascii') + payload
        return framed + bytes(self.slot_size - len(framed))

    def _decode(self, slot):
        if len(slot) != self.slot_size:
            raise JournalCorrupt('Journal has a torn slot')
        if slot == bytes(self.slot_size):
            return None
        try:
            size = int(slot[:8], 16)
            if not 0 < size <= self.slot_size - 72:
                raise ValueError()
            payload = slot[72:72 + size]
            if slot[8:72] != hashlib.sha256(payload).hexdigest().encode('ascii'):
                raise ValueError()
            if any(slot[72 + size:]):
                raise ValueError()
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise JournalCorrupt('Journal contains a corrupt or incomplete record') from None

    def _write_all(self, payload, offset):
        done = 0
        while done < len(payload):
            written = os.pwrite(self._fd, payload[done:], offset + done)
            if written <= 0:
                raise OSError('Journal write made no progress')
            done += written

    def _load(self):
        if self._fd is None:
            raise JournalError('Journal is closed')
        if os.fstat(self._fd).st_size != (self.capacity + 1) * self.slot_size:
            raise JournalCorrupt('Journal size does not match its reserved capacity')
        header = self._decode(os.pread(self._fd, self.slot_size, 0))
        if header != self._header():
            raise JournalCorrupt('Journal header does not match the requested format')
        items = []
        empty_seen = False
        for index in range(self.capacity):
            value = self._decode(os.pread(self._fd, self.slot_size, (index + 1) * self.slot_size))
            if value is None:
                empty_seen = True
            elif empty_seen:
                raise JournalCorrupt('Journal contains a record after an empty slot')
            else:
                items.append(value)
        return items

    def append(self, record):
        with self._mutex:
            if not self.usable or self._fd is None:
                raise JournalError('Journal is unavailable; further entries are prohibited')
            try:
                # Revalidate existing evidence rather than overwriting external damage.
                self._items = self._load()
                if len(self._items) >= self.capacity:
                    raise JournalCapacity('Reserved journal capacity is exhausted')
                slot = self._encode(record)
                self._write_all(slot, (len(self._items) + 1) * self.slot_size)
                os.fsync(self._fd)
                self._items.append(self._decode(slot))
            except BaseException:
                self.usable = False
                raise

    def records(self):
        with self._mutex:
            try:
                self._items = self._load()
            except BaseException:
                self.usable = False
                raise
            return copy.deepcopy(self._items)

    def __enter__(self):
        if self._fd is None or not self.usable:
            raise JournalError('Journal is unavailable')
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)


class ExecutionLock:
    """Independent account-wide lock; all executors must use the same path."""
    def __init__(self, path):
        self.path = Path(path)
        self._fd = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            if not stat.S_ISREG(os.fstat(self._fd).st_mode):
                raise JournalError('Execution lock must be a regular file')
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalLocked('Another executor holds the account lock') from None
        except BaseException:
            self.close()
            raise

    def close(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self):
        if self._fd is None:
            raise JournalError('Execution lock is closed')
        return self

    def __exit__(self, *args):
        self.close()
