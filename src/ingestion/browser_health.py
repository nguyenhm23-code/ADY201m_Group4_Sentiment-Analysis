"""Bound ChromeDriver transport waits independently of DOM/page-load waits."""
import time
from contextlib import contextmanager

from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from urllib3.exceptions import HTTPError


def connection_broken(error):
    return isinstance(error, (HTTPError, ConnectionError, TimeoutError, InvalidSessionIdException)) or (
        isinstance(error, WebDriverException) and any(part in str(error).lower() for part in
        ('disconnected', 'chrome not reachable', 'invalid session id', 'tab crashed')))


def configure_transport(driver, command_timeout=30, navigation_timeout=65):
    """One driver belongs to one worker; never change Selenium's global defaults.

    Selenium 4 keeps its live pool and per-request timeout on the executor.
    Clearing existing pools ensures GET /source cannot inherit urllib3 retries.
    """
    executor = driver.command_executor
    config = executor._client_config
    config.timeout = command_timeout
    config.init_args_for_pool_manager.setdefault('init_args_for_pool_manager', {})['retries'] = False
    if hasattr(executor, '_conn'):
        executor._conn.clear()
        executor._conn.connection_pool_kw['retries'] = False
    original = executor.execute

    def execute(command, params):
        if getattr(driver, '_transport_broken', False):
            raise ConnectionError('ChromeDriver connection failed; restart this browser before more commands.')
        timeout = navigation_timeout if command in ('get', 'refresh', 'goBack', 'goForward') else command_timeout
        deadline = getattr(driver, '_diagnostic_deadline', None)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Diagnostic time budget exhausted.')
            timeout = min(timeout, remaining)
        previous = config.timeout
        config.timeout = timeout
        try:
            return original(command, params)
        except Exception as exc:
            if connection_broken(exc):
                driver._transport_broken = True
            raise
        finally:
            config.timeout = previous

    executor.execute = execute
    return driver


@contextmanager
def diagnostic_budget(driver, seconds=12):
    previous = getattr(driver, '_diagnostic_deadline', None)
    driver._diagnostic_deadline = time.monotonic() + seconds
    try:
        yield
    finally:
        driver._diagnostic_deadline = previous
