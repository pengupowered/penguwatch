"""Webhooks in the settings store: the restrictions on what the page may set,
how a secret is resolved, how the dispatcher swaps its hooks while running, and that no URL or secret is ever logged.

The settings can be edited without an access token, so these are security rules. A page-set `secret_env` or
`secret_file` would make PenguWatch sign a body of the caller's choosing with ANY variable or file of the server and
send it to an address of the caller's choosing (an offline guessing oracle for that secret). The restrictions are
functions in penguwatch.webhooks that the settings validation calls for a new or changed value; the tests here pin
the functions, the resolution order, the live swap and the logging."""

import asyncio
import logging

import httpx
import pytest

from penguwatch import webhooks_component
from penguwatch.config import WebhookConfig
from penguwatch import notify, webhooks
from penguwatch.webhooks import (ADDRESS_NOT_ALLOWED, MIN_SECRET_LENGTH, NOT_SETTABLE, GuardedTransport, Webhook,
                                 WebhookDispatcher, blocked_host, check_hook_url, check_item_secrets, check_save,
                                 check_secret_env, check_secret_file, check_slot_secret, sign)
from tests.test_settings_live import FakeManager, rig  # noqa: F401  (fixture)

SECRET = "0123456789abcdef-stored-secret"
FILE_SECRET = "file-secret-0123456789abcdef"
ENV_SECRET = "env-secret-0123456789abcdef"
URL_TOKEN = "tok-9f8e7d6c5b4a"
URL = f"https://hooks.example.org/in/{URL_TOKEN}"
CANARY = "CANARY-must-not-be-echoed"


# --- the restrictions ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["PENGUWATCH_WEBHOOK_HA", "PENGUWATCH_WEBHOOK_A_1", "PENGUWATCH_WEBHOOK_" + "X" * 40])
def test_secret_env_accepts_names_made_for_webhooks(name):
    assert check_secret_env(name) is None


@pytest.mark.parametrize("name", [
    "PENGUWATCH_ACCESS_TOKEN", "PENGUWATCH_NOTIFY_URL", "SIMKL_ACCESS_TOKEN", "HOME", "PATH", "AWS_SECRET_ACCESS_KEY",
    "PENGUWATCH_WEBHOOK_", "PENGUWATCH_WEBHOOK", "penguwatch_webhook_ha", "PENGUWATCH_WEBHOOK_ha",
    "PENGUWATCH_WEBHOOK_" + "X" * 41, "XPENGUWATCH_WEBHOOK_A", "PENGUWATCH_WEBHOOK_A\n", " PENGUWATCH_WEBHOOK_A",
    "PENGUWATCH_WEBHOOK_A B", "", None])
def test_secret_env_refuses_any_other_variable(name):
    assert check_secret_env(name)


@pytest.mark.parametrize("path", ["/run/secrets/hook", "/run/secrets/team/hook.txt"])
def test_secret_file_accepts_imported_paths_under_run_secrets(path):
    assert check_secret_file(path) is None


@pytest.mark.parametrize("path", [
    "/etc/passwd", "/proc/self/environ", "/app/data/secrets.json", "/app/data/penguwatch.db", "/run/secrets",
    "/run/secrets/", "/run/secrets/../../etc/passwd", "/run/secrets/./hook", "/run/secrets//hook", "/run/secretsX/hook",
    "run/secrets/hook", "./run/secrets/hook", "/run/secrets/hook/", "/run/secrets/hook\0.txt", "", None])
def test_secret_file_refuses_everything_else(path):
    assert check_secret_file(path)


def test_the_stored_secret_needs_sixteen_characters_after_stripping():
    assert MIN_SECRET_LENGTH == 16
    assert check_slot_secret("x" * 16) is None
    assert check_slot_secret("  " + "x" * 15 + " \n") and check_slot_secret(" " * 20)       # resolve_secret strips too
    assert check_slot_secret("  " + "x" * 16 + " \n") is None
    assert check_slot_secret("x" * 15) and check_slot_secret("") and check_slot_secret(None)


@pytest.mark.parametrize("url", ["https://hooks.example.org/in/abc", "http://192.0.2.10:8123/api/webhook/x",
                                 "https://hooks.example.org:443/a?b=c@d", "http://[2001:db8::1]:8080/h"])
def test_hook_url_accepts_http_and_https_with_a_host(url):
    assert check_hook_url(url) is None


@pytest.mark.parametrize("url", [
    "https://user:pass@hooks.example.org/in", "https://user@hooks.example.org/in", "https://:pass@hooks.example.org/in",
    "ftp://hooks.example.org/in", "file:///etc/passwd", "javascript:alert(1)", "hooks.example.org/in", "https:///path",
    "https://hooks.example.org:99999/in", "https://hooks.example.org:abc/in", "https://hooks.example.org/a b",
    "https://hooks.example.org/\x00", "https://hooks.example.org/a\x7fb", "https://hooks.example.org/a\x85b",
    "https://hooks.example.org/a\u0090b", "", None])
def test_hook_url_refuses_other_schemes_no_host_credentials_bad_ports(url):
    assert check_hook_url(url)


def test_no_message_repeats_the_input():
    messages = [check_secret_env(CANARY), check_secret_file("/etc/" + CANARY), check_slot_secret(CANARY[:5]),
                check_hook_url("https://user:" + CANARY + "@h.example/x"), check_hook_url("ftp://" + CANARY),
                check_hook_url("https://h.example:" + CANARY + "/x")]
    assert all(m and CANARY not in m and CANARY[:5] not in m for m in messages), messages


def test_new_secret_env_and_file_are_restricted():
    """The page may not SET a secret_env or secret_file at all: not even one that looks harmless.
    Design 13.4: this is the test that must fail when the rule is removed."""
    for item in ({"secret_env": "PENGUWATCH_ACCESS_TOKEN"}, {"secret_env": "PENGUWATCH_WEBHOOK_HA"},
                 {"secret_file": "/etc/passwd"}, {"secret_file": "/run/secrets/ha"},
                 {"secret_file": "/run/secrets/../etc/passwd"}):
        (field_name,) = item
        assert check_item_secrets(item) == {field_name: NOT_SETTABLE}, item
    assert check_item_secrets({"secret_env": None, "secret_file": None}) == {}
    assert check_item_secrets({"secret_env": "", "secret_file": ""}) == {}
    assert check_item_secrets({}) == {}
    assert CANARY not in check_item_secrets({"secret_env": CANARY})["secret_env"]


def test_an_imported_value_is_kept_and_cleared_but_never_changed():
    legacy = {"secret_env": "MY_HOOK_SECRET", "secret_file": None}
    assert check_item_secrets({**legacy, "name": "renamed"}, stored=legacy) == {}                  # kept
    assert check_item_secrets({"secret_env": None, "secret_file": None}, stored=legacy) == {}       # cleared
    assert list(check_item_secrets({"secret_env": "PENGUWATCH_WEBHOOK_OK", "secret_file": None}, stored=legacy)) == ["secret_env"]
    assert list(check_item_secrets({"secret_env": "MY_HOOK_SECRET"}, stored=None)) == ["secret_env"]   # a new item
    legacy_file = {"secret_file": "/srv/hook-secret"}
    assert check_item_secrets(dict(legacy_file), stored=legacy_file) == {}
    assert list(check_item_secrets({"secret_file": "/run/secrets/other"}, stored=legacy_file)) == ["secret_file"]
    assert list(check_item_secrets({"secret_file": "/srv/hook-secret"}, stored=None)) == ["secret_file"]
    # the imported values themselves can still be validated by the import
    assert check_secret_env("PENGUWATCH_WEBHOOK_HA") is None and check_secret_file("/run/secrets/ha") is None


LEGACY = {"id": "home", "name": "Home", "secret_env": "MY_HOOK_SECRET", "secret_file": None}
PLAIN = {"id": "home", "name": "Home", "secret_env": None, "secret_file": None}


def test_a_hook_that_signs_with_an_imported_secret_cannot_be_moved_to_another_address():
    """H1: the signature of an imported secret must not follow an address the page typed."""
    assert check_save(LEGACY, LEGACY, url_changed=True, secret_slot_set=False)         # new URL, legacy secret kept
    assert check_save(LEGACY, LEGACY, url_changed=True, secret_slot_set=True)          # a stored secret next to it is no help
    assert check_save(None, LEGACY, url_changed=True, secret_slot_set=True)            # a new id that reads the same variable
    assert check_save(None, {**LEGACY, "id": "copy"}, url_changed=False, secret_slot_set=False)
    assert check_save(LEGACY, PLAIN, url_changed=True, secret_slot_set=False)          # cleared but no secret of its own
    assert CANARY not in (check_save(LEGACY, LEGACY, True, False) or "")


def test_check_save_allows_what_is_safe():
    assert check_save(LEGACY, LEGACY, url_changed=False, secret_slot_set=False) is None   # nothing about the address changes
    assert check_save(LEGACY, {**LEGACY, "name": "Renamed", "events": ["title.added"]}, False, False) is None
    assert check_save(LEGACY, PLAIN, url_changed=True, secret_slot_set=True) is None      # cleared and a stored secret set
    assert check_save(PLAIN, PLAIN, url_changed=True, secret_slot_set=False) is None      # nothing imported to protect
    assert check_save(None, PLAIN, url_changed=True, secret_slot_set=True) is None        # a new hook with a stored secret
    assert check_save(None, PLAIN, url_changed=True, secret_slot_set=False) is None


# --- SSRF: where a webhook or the digest may connect ---------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.1.2.3", "localhost", "LOCALHOST", "localhost.", "foo.localhost", "[::1]", "0.0.0.0", "[::]",
    "169.254.169.254", "169.254.0.1", "[fe80::1]", "224.0.0.1", "[ff02::1]", "0.1.2.3",
    "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "[::ffff:169.254.169.254]", "[::ffff:0.0.0.0]", "[::ffff:224.0.0.1]",
    "[::127.0.0.1]", "[64:ff9b::7f00:1]",
    "2130706433", "0x7f000001", "0x7f.1", "0177.0.0.1", "017700000001", "127.1", "2852039166", "0"])
def test_hook_url_refuses_this_machine_link_local_multicast_and_hidden_forms(host):
    assert check_hook_url(f"http://{host}/hook") == ADDRESS_NOT_ALLOWED, host
    assert blocked_host(host)


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "172.16.3.4", "172.31.255.1", "[fd00::5]", "8.8.8.8",
                                  "hooks.example.org", "homeassistant.local", "ha", "[2001:db8::1]", "[::ffff:192.168.1.5]",
                                  "1.2.3.4"])
def test_hook_url_allows_the_lan_and_public_hosts(host):
    assert check_hook_url(f"http://{host}:8123/api/webhook/x") is None, host


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, content=b"x")


def resolving(monkeypatch, *answers):
    """webhooks.resolve_host answers each call with the next list (the last repeats)."""
    calls = []

    async def resolve(host, port):
        calls.append((host, port))
        return list(answers[min(len(calls), len(answers)) - 1])
    monkeypatch.setattr(webhooks, "resolve_host", resolve)
    return calls


async def send_through(guard, url="https://hooks.example.org/in"):
    async with httpx.AsyncClient(transport=guard) as c:
        return await c.post(url, content=b"{}")


async def test_the_guard_connects_to_the_checked_address_keeping_host_and_sni(monkeypatch):
    inner = Recorder()
    resolving(monkeypatch, ["192.168.1.20"])
    assert (await send_through(GuardedTransport(inner))).status_code == 200
    (request,) = inner.requests
    assert request.url.host == "192.168.1.20" and request.headers["host"] == "hooks.example.org"
    assert request.extensions["sni_hostname"] == "hooks.example.org"


# --- the guard over real TLS ------------------------------------------------------------------------------------------
# The recording stub above shows what the guard hands to httpx; this shows the TLS that follows: a local HTTPS server
# with a throwaway CA and a certificate for a made-up name, the name resolved to it by a patched resolver, and the
# request sent through the guard to the checked IP. The certificate must still be verified against the NAME.

def _throwaway_ca_and_cert(folder, name):
    """(CA certificate PEM path, server certificate+key PEM path) for `name`, made now and never reused."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PenguWatch test CA (throwaway)")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(hours=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_path, chain_path = folder / "ca.pem", folder / "server.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    chain_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_path, chain_path


@pytest.fixture
def tls_receiver(tmp_path):
    """An HTTPS server on 127.0.0.1 with a certificate for hooks.test only; yields (port, CA path, received)."""
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ca_path, chain_path = _throwaway_ca_and_cert(tmp_path, "hooks.test")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            received.append({"host": self.headers["Host"], "path": self.path, "body": body})
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass  # a refused handshake (the wrong-name case) is expected here

    server = Server(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(chain_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], ca_path, received
    finally:
        server.shutdown()
        server.server_close()


async def test_the_guard_over_real_tls_verifies_the_certificate_against_the_name(monkeypatch, tls_receiver):
    import ssl

    port, ca_path, received = tls_receiver
    calls = resolving(monkeypatch, ["127.0.0.1"])           # every name resolves to the test server
    real_blocked = webhooks.blocked_host
    monkeypatch.setattr(webhooks, "blocked_host", lambda host: host != "127.0.0.1" and real_blocked(host))
    trusting = ssl.create_default_context(cafile=str(ca_path))

    def guard(context=trusting):
        return GuardedTransport(httpx.AsyncHTTPTransport(verify=context, trust_env=False))

    # the right name: the certificate verifies, the request arrives, the Host header is the name
    res = await send_through(guard(), f"https://hooks.test:{port}/in")
    assert res.status_code == 204
    assert received == [{"host": f"hooks.test:{port}", "path": "/in", "body": b"{}"}]
    assert calls == [("hooks.test", port)]
    # another name that resolves to the same server: its certificate is not for that name, so TLS refuses
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED|[Hh]ostname mismatch"):
        await send_through(guard(), f"https://wrong.test:{port}/in")
    # the right name, but a client that does not trust the throwaway CA: verification is really on
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await send_through(guard(ssl.create_default_context()), f"https://hooks.test:{port}/in")
    # the test address itself is still refused when the guard's check is the real one
    monkeypatch.setattr(webhooks, "blocked_host", real_blocked)
    with pytest.raises(httpx.ConnectError, match="address not allowed"):
        await send_through(guard(), f"https://hooks.test:{port}/in")
    assert len(received) == 1


@pytest.mark.parametrize("answer", [["127.0.0.1"], ["::1"], ["169.254.169.254"], ["0.0.0.0"], ["::ffff:127.0.0.1"],
                                    ["192.168.1.20", "127.0.0.1"], ["127.0.0.1", "192.168.1.20"], []])
async def test_the_guard_refuses_a_name_that_resolves_to_this_machine(monkeypatch, answer):
    inner = Recorder()
    resolving(monkeypatch, answer)
    with pytest.raises(httpx.ConnectError):
        await send_through(GuardedTransport(inner))
    assert inner.requests == []


async def test_dns_rebinding_is_closed_each_request_is_checked_at_connect_time(monkeypatch):
    """The name passes the address check when the page saves it, then points at this machine."""
    inner = Recorder()
    calls = resolving(monkeypatch, ["203.0.113.7"], ["127.0.0.1"])
    guard = GuardedTransport(inner)
    assert (await send_through(guard)).status_code == 200
    with pytest.raises(httpx.ConnectError):
        await send_through(guard)
    assert len(inner.requests) == 1 and len(calls) == 2


async def test_the_guard_refuses_ip_literals_without_asking_dns():
    inner = Recorder()
    for url in ("http://127.0.0.1:8080/x", "http://169.254.169.254/latest", "http://[::1]/x"):
        with pytest.raises(httpx.ConnectError):
            await send_through(GuardedTransport(inner), url)
    assert inner.requests == []
    assert (await send_through(GuardedTransport(inner), "http://192.168.1.5:8123/x")).status_code == 200
    assert inner.requests[0].url.host == "192.168.1.5"


async def test_the_webhook_dispatcher_and_the_digest_use_the_guard_by_default():
    hook = Webhook(name="x", url="http://127.0.0.1:9/in", events=frozenset(["title.added"]))
    d = WebhookDispatcher([hook], retry_delays=(), sleep=no_sleep)
    assert isinstance(d._client._transport, GuardedTransport)
    d.publish("title.added", {})
    assert not await d.deliver(hook, *hook.queue.get_nowait())
    assert hook.last_error == "could not connect"
    await d.close()
    with pytest.raises(httpx.ConnectError):
        await notify.Notifier("http://169.254.169.254/topic").send(notify.Event(key="k", title="t", message="m"))


async def test_the_digest_client_resolves_and_checks_the_address_before_connecting(monkeypatch):
    calls = resolving(monkeypatch, ["127.0.0.1"])      # only the guard asks the resolver
    with pytest.raises(httpx.ConnectError):
        await notify.Notifier("https://ntfy.example.com/topic").send(notify.Event(key="k", title="t", message="m"))
    assert calls == [("ntfy.example.com", 443)]


async def test_last_error_is_a_coarse_category_with_no_code_or_exception_name():
    labels = set()
    for status in (200, 302, 400, 404, 429, 500, 503):
        hook = Webhook(name="x", url=URL, events=frozenset(["title.added"]))
        d = WebhookDispatcher([hook], transport=httpx.MockTransport(Receiver(status)), retry_delays=(), sleep=no_sleep)
        d.publish("title.added", {})
        await d.deliver(hook, *hook.queue.get_nowait())
        labels.add(hook.last_error)
    for exc in (httpx.ConnectError("c"), httpx.ReadTimeout("t"), httpx.RemoteProtocolError("p")):
        hook = Webhook(name="x", url=URL, events=frozenset(["title.added"]))
        d = WebhookDispatcher([hook], transport=httpx.MockTransport(Receiver(raises=exc)), retry_delays=(), sleep=no_sleep)
        d.publish("title.added", {})
        await d.deliver(hook, *hook.queue.get_nowait())
        labels.add(hook.last_error)
    assert labels == {None, "redirect not followed", "refused by the receiver (HTTP 4xx)", "server error (HTTP 5xx)",
                      "could not connect", "timed out"}


# --- resolving the secret -----------------------------------------------------------------------------------------

def cfg(**over):
    return WebhookConfig(**{"id": "home", "name": "home", "url": URL, **over})


def test_secret_resolution_order_is_env_then_file_then_the_stored_slot(tmp_path):
    f = tmp_path / "s"
    f.write_text(FILE_SECRET)
    stored = {"webhook.home.secret": SECRET}.get
    environ = {"HOOK": ENV_SECRET}
    assert webhooks_component.resolve_secret(cfg(secret_env="HOOK"), environ, stored) == (ENV_SECRET.encode(), None)
    assert webhooks_component.resolve_secret(cfg(secret_file=str(f)), environ, stored) == (FILE_SECRET.encode(), None)
    assert webhooks_component.resolve_secret(cfg(), environ, stored) == (SECRET.encode(), None)
    assert webhooks_component.resolve_secret(cfg(), environ, None) == (None, None)               # unsigned, as before
    assert webhooks_component.resolve_secret(cfg(), environ, {}.get) == (None, None)             # no slot set
    # an env name that is empty does not fall back to the slot: configured to be signed by it, it stays off
    secret, why = webhooks_component.resolve_secret(cfg(secret_env="HOOK"), {}, stored)
    assert secret is None and why == "environment variable HOOK is not set"


def test_a_short_stored_secret_keeps_the_hook_off_and_the_reason_has_no_value():
    secret, why = webhooks_component.resolve_secret(cfg(), {}, {"webhook.home.secret": "short-secret"}.get)
    assert secret is None and "shorter than 16" in why and "short-secret" not in why
    assert webhooks_component.resolve_secret(cfg(id="other"), {}, {"webhook.home.secret": SECRET}.get) == (None, None)


def test_build_hooks_gives_each_hook_its_id_url_events_and_secret():
    hooks = webhooks_component.build_hooks([cfg(events=["title.added"]), cfg(id="b", name="b", url=URL + "2")],
                                           stored={"webhook.home.secret": SECRET}.get)
    assert [(h.id, h.name, h.url, h.events, h.secret, h.disabled_reason) for h in hooks] == [
        ("home", "home", URL, frozenset(["title.added"]), SECRET.encode(), None),
        ("b", "b", URL + "2", hooks[1].events, None, None)]


# --- the signature never gives a secret away -----------------------------------------------------------------------

class Receiver:
    def __init__(self, status=200, raises=None):
        self.status, self.raises, self.requests = status, raises, []

    def __call__(self, request):
        self.requests.append(request)
        if self.raises:
            raise self.raises
        return httpx.Response(self.status, content=b"x")


async def no_sleep(_):
    return None


async def test_a_delivery_carries_a_signature_from_the_stored_secret_and_the_secret_is_nowhere_else():
    rx = Receiver()
    hook = webhooks_component.build_hooks([cfg()], stored={"webhook.home.secret": SECRET}.get)[0]
    d = WebhookDispatcher([hook], transport=httpx.MockTransport(rx), sleep=no_sleep)
    assert d.publish("title.added", {"title": {"id": 1, "title": "T"}}) == 1
    await d.deliver(hook, *hook.queue.get_nowait())
    request = rx.requests[0]
    stamp = request.headers["x-penguwatch-timestamp"]
    assert request.headers["x-penguwatch-signature"] == sign(SECRET.encode(), stamp, request.content)
    everything = repr(hook) + repr(d.describe()) + repr(d.recent) + str(dict(request.headers)) + request.content.decode()
    assert SECRET not in everything and "secret" not in {k for k in hook.describe()}
    await d.close()


# --- set_hooks: the dispatcher swaps its hooks while running -------------------------------------------------------

def hook(id="home", url=URL, events=("title.added",), secret=None, name=None):
    return Webhook(name=name or id, url=url, events=frozenset(events), secret=secret, id=id)


class Spawner:
    def __init__(self):
        self.tasks = []

    def __call__(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


async def test_an_unchanged_hook_keeps_its_object_queue_and_counters_and_its_worker():
    spawn = Spawner()
    a, b = hook("a", secret=b"s" * 16), hook("b", url=URL + "b")
    d = WebhookDispatcher([a, b], transport=httpx.MockTransport(Receiver()), sleep=no_sleep)
    d.start(spawn)
    assert len(spawn.tasks) == 2
    a.delivered, a.failed = 5, 2
    a.queue.put_nowait(("title.added", "id", b"{}"))
    first_task = d._tasks[id(a)]
    new_a = hook("a", secret=b"s" * 16)
    d.set_hooks([new_a, hook("b", url=URL + "b2")], spawn)       # b's address changed, a is the same
    assert d.hooks[0] is a and (a.delivered, a.failed) == (5, 2) and d._tasks[id(a)] is first_task
    assert d.hooks[1] is not b and len(spawn.tasks) == 3          # b got a new worker
    await asyncio.sleep(0)
    assert spawn.tasks[1].cancelled() or spawn.tasks[1].done()    # and the old one ended
    for t in spawn.tasks:
        t.cancel()
    await asyncio.gather(*spawn.tasks, return_exceptions=True)
    await d.close()


async def test_a_removed_hook_loses_its_worker_and_its_queue_and_a_new_one_gets_a_worker():
    spawn = Spawner()
    a = hook("a")
    d = WebhookDispatcher([a], transport=httpx.MockTransport(Receiver()), sleep=no_sleep)
    d.start(spawn)
    a.queue.put_nowait(("title.added", "x", b"{}"))
    d.set_hooks([hook("c", url=URL + "c")], spawn)
    await asyncio.sleep(0)
    assert spawn.tasks[0].cancelled() and a.queue.empty()
    assert [h.id for h in d.hooks] == ["c"] and len(spawn.tasks) == 2 and not spawn.tasks[1].done()
    d.set_hooks([], spawn)
    await asyncio.sleep(0)
    assert d.hooks == [] and spawn.tasks[1].cancelled()
    await d.close()


async def test_a_secret_or_events_change_replaces_the_hook_and_a_rename_alone_does_not():
    spawn = Spawner()
    a = hook("a", secret=b"s" * 16)
    d = WebhookDispatcher([a], transport=httpx.MockTransport(Receiver()), sleep=no_sleep)
    d.start(spawn)
    d.set_hooks([hook("a", secret=b"t" * 16)], spawn)
    assert d.hooks[0] is not a
    kept = d.hooks[0]
    d.set_hooks([hook("a", secret=b"t" * 16, name="Renamed")], spawn)
    assert d.hooks[0] is kept and kept.name == "Renamed"
    d.set_hooks([hook("a", secret=b"t" * 16, events=("sync.failed",), name="Renamed")], spawn)
    assert d.hooks[0] is not kept and d.hooks[0].events == frozenset(["sync.failed"])
    for t in spawn.tasks:
        t.cancel()
    await asyncio.gather(*spawn.tasks, return_exceptions=True)
    await d.close()


async def test_a_disabled_hook_gets_no_worker_and_before_start_the_list_is_only_replaced():
    spawn = Spawner()
    d = WebhookDispatcher([], transport=httpx.MockTransport(Receiver()), sleep=no_sleep)
    d.set_hooks([hook("a")])                                  # not started, no spawn: nothing runs yet
    assert [h.id for h in d.hooks] == ["a"] and d._tasks == {}
    d.start(spawn)
    assert len(spawn.tasks) == 1
    off = hook("b")
    off.disabled_reason = "environment variable X is not set"
    d.set_hooks([hook("a"), off], spawn)
    assert len(spawn.tasks) == 1 and d.publish("title.added", {}) == 1
    for t in spawn.tasks:
        t.cancel()
    await asyncio.gather(*spawn.tasks, return_exceptions=True)
    await d.close()


# --- live through the hub ------------------------------------------------------------------------------------------

ITEM = {"id": "home", "name": "Home", "events": ["title.added"], "secret_env": None, "secret_file": None}


async def test_saving_a_webhook_applies_with_no_restart_signed_by_the_stored_secret(rig):
    rx = Receiver()
    rig.dispatcher._client = httpx.AsyncClient(transport=httpx.MockTransport(rx), follow_redirects=False)
    rig.dispatcher.start(rig.ctx.spawn)
    assert rig.dispatcher.hooks == []
    failed = await rig.manager.put(items=[ITEM], slots={"webhook.home.url": URL, "webhook.home.secret": SECRET})
    assert failed == []
    (live,) = rig.dispatcher.hooks
    assert (live.id, live.name, live.url, live.secret) == ("home", "Home", URL, SECRET.encode())
    assert rig.dispatcher.publish("title.added", {"x": 1}) == 1
    for _ in range(100):
        await asyncio.sleep(0.01)
        if rx.requests:
            break
    request = rx.requests[0]
    assert request.headers["x-penguwatch-signature"] == sign(
        SECRET.encode(), request.headers["x-penguwatch-timestamp"], request.content)
    # a new secret replaces the hook and signs from the next event on; the counters of the old object go with it
    await rig.manager.put(slots={"webhook.home.secret": SECRET[::-1]})
    assert rig.dispatcher.hooks[0] is not live and rig.dispatcher.hooks[0].secret == SECRET[::-1].encode()
    # clearing the address leaves the item without a hook (state: address needed)
    await rig.manager.put(slots={"webhook.home.url": ""})
    assert rig.dispatcher.hooks == [] and rig.dispatcher.publish("title.added", {}) == 0
    # removing the item
    await rig.manager.put(slots={"webhook.home.url": URL})
    assert len(rig.dispatcher.hooks) == 1
    await rig.manager.put(items=[])
    assert rig.dispatcher.hooks == []


async def test_a_secret_of_a_removed_name_or_a_short_one_never_signs_and_the_hook_stays_off(rig, monkeypatch):
    monkeypatch.delenv("PENGUWATCH_WEBHOOK_HA", raising=False)
    await rig.manager.put(items=[{**ITEM, "secret_env": "PENGUWATCH_WEBHOOK_HA"}],
                          slots={"webhook.home.url": URL, "webhook.home.secret": SECRET})
    (hook_,) = rig.dispatcher.hooks
    assert hook_.disabled_reason == "environment variable PENGUWATCH_WEBHOOK_HA is not set" and hook_.secret is None
    assert rig.dispatcher.publish("title.added", {}) == 0              # the item names a variable: the slot is not a fallback
    await rig.manager.put(items=[ITEM], slots={"webhook.home.secret": "too-short"})
    assert rig.dispatcher.hooks[0].disabled_reason and rig.dispatcher.hooks[0].secret is None


# --- nothing secret is logged --------------------------------------------------------------------------------------

async def test_no_address_or_secret_reaches_a_log_line(rig, caplog, monkeypatch):
    """Everything the settings apply path logs, with an unreadable secret, a disabled hook, a failing delivery that
    quotes the address in its error, and an applier that raises with the address in its message."""
    monkeypatch.delenv("PENGUWATCH_WEBHOOK_GONE", raising=False)
    secrets_ = [URL_TOKEN, SECRET, ENV_SECRET, FILE_SECRET, "https://ntfy.example.com/family-topic", "family-topic"]
    rx = Receiver(raises=httpx.ConnectError(f"cannot reach {URL}?key={SECRET}"))
    rig.dispatcher._client = httpx.AsyncClient(transport=httpx.MockTransport(rx), follow_redirects=False)
    rig.dispatcher._sleep = no_sleep
    rig.dispatcher.retry_delays = (0,)
    rig.dispatcher.start(rig.ctx.spawn)
    # DEBUG too: the hub logs the frames of a failed applier at DEBUG, never the exception's message
    with caplog.at_level(logging.DEBUG):
        await rig.manager.put(items=[ITEM, {**ITEM, "id": "gone", "name": "Gone", "secret_env": "PENGUWATCH_WEBHOOK_GONE"},
                                     {**ITEM, "id": "short", "name": "Short"}],
                              slots={"webhook.home.url": URL, "webhook.home.secret": SECRET,
                                     "webhook.gone.url": URL + "g", "webhook.short.url": URL + "s",
                                     "webhook.short.secret": "short", "notify.url": "https://ntfy.example.com/family-topic"})
        rig.dispatcher.publish("title.added", {"x": 1})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if rig.dispatcher.hooks[0].failed:
                break
        assert await rig.dispatcher.send_test(rig.dispatcher.hooks[0]) is False

        def boom(hooks, spawn):
            raise ValueError(f"bad {URL} {SECRET}")
        monkeypatch.setattr(rig.dispatcher, "set_hooks", boom)
        failed = await rig.manager.put(slots={"webhook.home.secret": ENV_SECRET + "x"})
    assert failed == ["webhooks.hooks"]
    assert rig.dispatcher.hooks[0].failed >= 1           # the failures really happened and were logged
    assert "Webhook 'Gone' is disabled" in caplog.text and "delivery failed" in caplog.text
    for value in secrets_:
        assert value not in caplog.text, value


# --- changed hooks: queued events, keys, and a delivery in flight --------------------------------------------------

def queued(h, *types):
    for n, t in enumerate(types):
        h.queue.put_nowait((t, f"id{n}", b"{}"))


async def test_a_changed_hook_counts_the_events_it_drops():
    d = WebhookDispatcher([hook("a")], transport=httpx.MockTransport(Receiver()), sleep=no_sleep)
    old = d.hooks[0]
    queued(old, "title.added", "title.added", "title.added")
    d.set_hooks([hook("a", url=URL + "2")])
    assert d.hooks[0].dropped == 3 and d.hooks[0].queue.empty()        # another address: nothing is carried over
    d.set_hooks([])                                                    # removed: counted in the metric, no object left
    await d.close()


async def test_when_only_the_events_change_the_still_subscribed_queued_events_move_over():
    d = WebhookDispatcher([hook("a", events=("title.added", "sync.failed", "episode.watched"))],
                          transport=httpx.MockTransport(Receiver()), sleep=no_sleep)
    queued(d.hooks[0], "title.added", "sync.failed", "episode.watched", "title.added")
    d.set_hooks([hook("a", events=("title.added", "episode.watched"))])
    new = d.hooks[0]
    assert [new.queue.get_nowait()[0] for _ in range(new.queue.qsize())] == ["title.added", "episode.watched", "title.added"]
    assert new.dropped == 1                                            # the sync.failed one
    # a changed secret is another target: nothing moves
    queued(new, "title.added")
    d.set_hooks([hook("a", events=("title.added", "episode.watched"), secret=b"s" * 16)])
    assert d.hooks[0].queue.empty() and d.hooks[0].dropped == 1
    await d.close()


async def test_hooks_pair_by_id_only_and_an_id_less_hook_has_its_own_namespace():
    d = WebhookDispatcher([hook("a", name="Same"), hook(None, name="Same", url=URL + "legacy")],
                          transport=httpx.MockTransport(Receiver()), sleep=no_sleep)
    a, legacy = d.hooks
    d.set_hooks([hook("b", name="Same"), hook(None, name="Same", url=URL + "legacy"), hook("a", name="Other")])
    assert d.hooks[1] is legacy and d.hooks[2] is a and a.name == "Other"       # "b" took neither by name
    assert d.hooks[0] is not a and d.hooks[0].id == "b"
    d.set_hooks([hook("a", name="Same", url=URL + "x")])                         # a name equal to a legacy one pairs nothing
    assert d.hooks[0] is not a
    d.set_hooks([hook(None, name="b")])                                          # id-less "b" ...
    legacy_b = d.hooks[0]
    d.set_hooks([hook("b", name="other")])                                        # ... is not the hook with id "b"
    assert d.hooks[0] is not legacy_b
    await d.close()


async def test_a_delivery_sleeping_between_retries_never_sends_to_the_old_address():
    release, slept = asyncio.Event(), asyncio.Event()

    async def sleep(_):
        slept.set()
        await release.wait()
    rx = Receiver(500)
    old = hook("a", url="https://old.example.org/in")
    d = WebhookDispatcher([old], transport=httpx.MockTransport(rx), sleep=sleep, retry_delays=(1.0,))
    spawn = Spawner()
    d.start(spawn)
    d.publish("title.added", {})
    await asyncio.wait_for(slept.wait(), 2)                       # attempt 1 failed; the worker is waiting to retry
    assert [r.url.host for r in rx.requests] == ["old.example.org"]
    d.set_hooks([hook("a", url="https://new.example.org/in")], spawn)
    release.set()
    await asyncio.sleep(0.1)
    assert [r.url.host for r in rx.requests] == ["old.example.org"]            # no second attempt to the old address
    assert spawn.tasks[0].cancelled()
    for t in spawn.tasks:
        t.cancel()
    await asyncio.gather(*spawn.tasks, return_exceptions=True)
    await d.close()


# --- a bad legacy secret file never breaks the hooks around it -----------------------------------------------------

def test_a_nul_in_secret_file_is_refused_by_the_models_and_does_not_crash_resolution():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WebhookConfig(name="x", url=URL, secret_file="/run/secrets/a\0b")
    bad = WebhookConfig.model_construct(id="x", name="x", url=URL, events=["title.added"], secret_env=None,
                                        secret_file="/run/secrets/a\0b")
    secret, why = webhooks_component.resolve_secret(bad, {})
    assert secret is None and why and "\0" not in why and "a\0b" not in why


def test_a_secret_file_path_is_named_only_when_it_looks_like_a_path(tmp_path):
    secret, why = webhooks_component.resolve_secret(cfg(secret_file=str(tmp_path / "nope")), {})
    assert str(tmp_path) in why                                       # an ordinary path is shown
    for odd in ("/run/secrets/" + CANARY + " <script>", "/run/secrets/" + "x" * 300):
        secret, why = webhooks_component.resolve_secret(cfg(secret_file=odd), {})
        assert secret is None and CANARY not in why and "<script>" not in why and "secret_file setting" in why


def test_one_bad_hook_is_contained_by_build_hooks(monkeypatch, caplog):
    real = webhooks_component.resolve_secret

    def resolve(c, environ=None, stored=None):
        if c.id == "bad":
            raise ValueError(f"boom {URL} {SECRET}")
        return real(c, environ, stored)
    monkeypatch.setattr(webhooks_component, "resolve_secret", resolve)
    with caplog.at_level(logging.DEBUG):
        hooks = webhooks_component.build_hooks([cfg(id="a", name="a"), cfg(id="bad", name="bad", url=URL + "b"),
                                                cfg(id="c", name="c", url=URL + "c")],
                                               stored={"webhook.a.secret": SECRET}.get)
    assert [h.id for h in hooks] == ["a", "bad", "c"]
    assert hooks[0].secret == SECRET.encode() and hooks[2].disabled_reason is None
    assert hooks[1].disabled_reason and hooks[1].secret is None and hooks[1].url == ""
    assert URL_TOKEN not in caplog.text and SECRET not in caplog.text and "ValueError" in caplog.text
    d = WebhookDispatcher(hooks, transport=httpx.MockTransport(Receiver()))
    assert d.publish("title.added", {}) == 2                           # the others still take events


# --- the hub never logs an applier's message, not even at DEBUG ------------------------------------------------------

async def test_the_hub_logs_the_frames_of_a_failed_applier_and_not_its_message(caplog):
    from penguwatch.settings.hub import Change, SettingsHub
    hub = SettingsHub()

    @hub.on(keys=["display.theme"], name="boom")
    def boom(change):
        raise ValueError(f"quoted {URL} {SECRET}")
    with caplog.at_level(logging.DEBUG):
        assert await hub.publish(Change.of(keys=["display.theme"])) == ["boom"]
    assert "ValueError" in caplog.text and "test_settings_webhooks.py" in caplog.text    # type and frames
    assert URL_TOKEN not in caplog.text and SECRET not in caplog.text
