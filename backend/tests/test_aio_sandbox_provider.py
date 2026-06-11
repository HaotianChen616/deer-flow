"""Tests for AioSandboxProvider mount helpers."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from deerflow.config.paths import Paths, join_host_path
from deerflow.runtime.user_context import reset_current_user, set_current_user

# ── ensure_thread_dirs ───────────────────────────────────────────────────────


def test_ensure_thread_dirs_creates_acp_workspace(tmp_path):
    """ACP workspace directory must be created alongside user-data dirs."""
    paths = Paths(base_dir=tmp_path)
    paths.ensure_thread_dirs("thread-1")

    assert (tmp_path / "threads" / "thread-1" / "user-data" / "workspace").exists()
    assert (tmp_path / "threads" / "thread-1" / "user-data" / "uploads").exists()
    assert (tmp_path / "threads" / "thread-1" / "user-data" / "outputs").exists()
    assert (tmp_path / "threads" / "thread-1" / "acp-workspace").exists()


def test_ensure_thread_dirs_acp_workspace_is_world_writable(tmp_path):
    """ACP workspace must be chmod 0o777 so the ACP subprocess can write into it."""
    paths = Paths(base_dir=tmp_path)
    paths.ensure_thread_dirs("thread-2")

    acp_dir = tmp_path / "threads" / "thread-2" / "acp-workspace"
    mode = oct(acp_dir.stat().st_mode & 0o777)
    assert mode == oct(0o777)


def test_host_thread_dir_rejects_invalid_thread_id(tmp_path):
    paths = Paths(base_dir=tmp_path)

    with pytest.raises(ValueError, match="Invalid thread_id"):
        paths.host_thread_dir("../escape")


# ── _get_thread_mounts ───────────────────────────────────────────────────────


def _make_provider(tmp_path):
    """Build a minimal AioSandboxProvider instance without starting the idle checker."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    with patch.object(aio_mod.AioSandboxProvider, "_start_idle_checker"):
        provider = aio_mod.AioSandboxProvider.__new__(aio_mod.AioSandboxProvider)
        provider._config = {}
        provider._sandboxes = {}
        provider._lock = MagicMock()
        provider._idle_checker_stop = MagicMock()
    return provider


def test_get_thread_mounts_includes_acp_workspace(tmp_path, monkeypatch):
    """_get_thread_mounts must include /mnt/acp-workspace (read-only) for docker sandbox."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    monkeypatch.setattr(aio_mod, "get_paths", lambda: Paths(base_dir=tmp_path))
    monkeypatch.setattr(aio_mod, "get_effective_user_id", lambda: None)

    mounts = aio_mod.AioSandboxProvider._get_thread_mounts("thread-3")

    container_paths = {m[1]: (m[0], m[2]) for m in mounts}

    assert "/mnt/acp-workspace" in container_paths, "ACP workspace mount is missing"
    expected_host = str(tmp_path / "threads" / "thread-3" / "acp-workspace")
    actual_host, read_only = container_paths["/mnt/acp-workspace"]
    assert actual_host == expected_host
    assert read_only is True, "ACP workspace should be read-only inside the sandbox"


def test_get_thread_mounts_includes_user_data_dirs(tmp_path, monkeypatch):
    """Baseline: user-data mounts must still be present after the ACP workspace change."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    monkeypatch.setattr(aio_mod, "get_paths", lambda: Paths(base_dir=tmp_path))

    mounts = aio_mod.AioSandboxProvider._get_thread_mounts("thread-4")
    container_paths = {m[1] for m in mounts}

    assert "/mnt/user-data/workspace" in container_paths
    assert "/mnt/user-data/uploads" in container_paths
    assert "/mnt/user-data/outputs" in container_paths


def test_join_host_path_preserves_windows_drive_letter_style():
    base = r"C:\Users\demo\deer-flow\backend\.deer-flow"

    joined = join_host_path(base, "threads", "thread-9", "user-data", "outputs")

    assert joined == r"C:\Users\demo\deer-flow\backend\.deer-flow\threads\thread-9\user-data\outputs"


def test_get_thread_mounts_preserves_windows_host_path_style(tmp_path, monkeypatch):
    """Docker bind mount sources must keep Windows-style paths intact."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    monkeypatch.setenv("DEER_FLOW_HOST_BASE_DIR", r"C:\Users\demo\deer-flow\backend\.deer-flow")
    monkeypatch.setattr(aio_mod, "get_paths", lambda: Paths(base_dir=tmp_path))
    monkeypatch.setattr(aio_mod, "get_effective_user_id", lambda: None)

    mounts = aio_mod.AioSandboxProvider._get_thread_mounts("thread-10")

    container_paths = {container_path: host_path for host_path, container_path, _ in mounts}

    assert container_paths["/mnt/user-data/workspace"] == r"C:\Users\demo\deer-flow\backend\.deer-flow\threads\thread-10\user-data\workspace"
    assert container_paths["/mnt/user-data/uploads"] == r"C:\Users\demo\deer-flow\backend\.deer-flow\threads\thread-10\user-data\uploads"
    assert container_paths["/mnt/user-data/outputs"] == r"C:\Users\demo\deer-flow\backend\.deer-flow\threads\thread-10\user-data\outputs"
    assert container_paths["/mnt/acp-workspace"] == r"C:\Users\demo\deer-flow\backend\.deer-flow\threads\thread-10\acp-workspace"


def test_discover_or_create_only_unlocks_when_lock_succeeds(tmp_path, monkeypatch):
    """Unlock should not run if exclusive locking itself fails."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._discover_or_create_with_lock = aio_mod.AioSandboxProvider._discover_or_create_with_lock.__get__(
        provider,
        aio_mod.AioSandboxProvider,
    )

    monkeypatch.setattr(aio_mod, "get_paths", lambda: Paths(base_dir=tmp_path))
    monkeypatch.setattr(
        aio_mod,
        "_lock_file_exclusive",
        lambda _lock_file: (_ for _ in ()).throw(RuntimeError("lock failed")),
    )

    unlock_calls: list[object] = []
    monkeypatch.setattr(
        aio_mod,
        "_unlock_file",
        lambda lock_file: unlock_calls.append(lock_file),
    )

    with patch.object(provider, "_create_sandbox", return_value="sandbox-id"):
        with pytest.raises(RuntimeError, match="lock failed"):
            provider._discover_or_create_with_lock("thread-5", "sandbox-5")

    assert unlock_calls == []


@pytest.mark.anyio
async def test_acquire_async_uses_async_readiness_polling(monkeypatch):
    """AioSandboxProvider async creation must not use sync readiness polling."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(None)
    provider._config = {"replicas": 3}
    provider._thread_locks = {}
    provider._warm_pool = {}
    provider._sandbox_infos = {}
    provider._thread_sandboxes = {}
    provider._last_activity = {}
    provider._lock = aio_mod.threading.Lock()
    provider._backend = SimpleNamespace(
        create=MagicMock(return_value=aio_mod.SandboxInfo(sandbox_id="sandbox-async", sandbox_url="http://sandbox")),
        destroy=MagicMock(),
        discover=MagicMock(return_value=None),
        is_alive=MagicMock(return_value=True),
    )

    async_readiness_calls: list[tuple[str, int]] = []

    async def fake_wait_for_sandbox_ready_async(sandbox_url: str, timeout: int = 30, poll_interval: float = 1.0) -> bool:
        async_readiness_calls.append((sandbox_url, timeout))
        return True

    monkeypatch.setattr(aio_mod, "wait_for_sandbox_ready_async", fake_wait_for_sandbox_ready_async)
    monkeypatch.setattr(
        aio_mod,
        "wait_for_sandbox_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sync readiness should not be used")),
    )

    sandbox_id = await provider._create_sandbox_async("thread-async", "sandbox-async")

    assert sandbox_id == "sandbox-async"
    assert async_readiness_calls == [("http://sandbox", 60)]
    assert provider._backend.destroy.call_count == 0
    assert provider._thread_sandboxes["thread-async"] == "sandbox-async"


@pytest.mark.anyio
async def test_discover_or_create_with_lock_async_offloads_lock_file_open_and_close(tmp_path, monkeypatch):
    """Async lock path must not open or close lock files on the event loop."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._discover_or_create_with_lock_async = aio_mod.AioSandboxProvider._discover_or_create_with_lock_async.__get__(
        provider,
        aio_mod.AioSandboxProvider,
    )
    provider._thread_locks = {}
    provider._warm_pool = {}
    provider._sandbox_infos = {"sandbox-async-lock": aio_mod.SandboxInfo(sandbox_id="sandbox-async-lock", sandbox_url="http://sandbox")}
    provider._thread_sandboxes = {"thread-async-lock": "sandbox-async-lock"}
    provider._sandboxes = {"sandbox-async-lock": aio_mod.AioSandbox(id="sandbox-async-lock", base_url="http://sandbox")}
    provider._last_activity = {}
    provider._lock = aio_mod.threading.Lock()
    provider._backend = SimpleNamespace(discover=MagicMock(return_value=None), is_alive=MagicMock(return_value=True))

    monkeypatch.setattr(aio_mod, "get_paths", lambda: Paths(base_dir=tmp_path))

    to_thread_calls: list[object] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(aio_mod.asyncio, "to_thread", fake_to_thread)

    sandbox_id = await provider._discover_or_create_with_lock_async("thread-async-lock", "sandbox-async-lock")

    assert sandbox_id == "sandbox-async-lock"
    assert aio_mod._open_lock_file in to_thread_calls
    assert any(getattr(func, "__name__", "") == "close" for func in to_thread_calls)


@pytest.mark.anyio
async def test_acquire_thread_lock_async_uses_dedicated_executor(monkeypatch):
    """Per-thread lock waits should not consume the default asyncio.to_thread pool."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    lock = aio_mod.threading.Lock()

    async def fail_to_thread(*_args, **_kwargs):
        raise AssertionError("thread-lock acquisition must not use asyncio.to_thread")

    monkeypatch.setattr(aio_mod.asyncio, "to_thread", fail_to_thread)

    await aio_mod._acquire_thread_lock_async(lock)
    try:
        assert not lock.acquire(blocking=False)
    finally:
        lock.release()


@pytest.mark.anyio
async def test_acquire_async_cancellation_does_not_leak_thread_lock(tmp_path):
    """Cancelled async lock waiters must not leave the per-thread lock held."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._thread_locks = {}
    provider._warm_pool = {}
    provider._sandbox_infos = {}
    provider._thread_sandboxes = {}
    provider._last_activity = {}
    provider._lock = aio_mod.threading.Lock()

    thread_id = "thread-cancel-lock"
    thread_lock = provider._get_thread_lock(thread_id)
    thread_lock.acquire()

    task = asyncio.create_task(provider.acquire_async(thread_id))
    await asyncio.sleep(0.05)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    thread_lock.release()
    deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < deadline:
        acquired = thread_lock.acquire(blocking=False)
        if acquired:
            thread_lock.release()
            return
        await asyncio.sleep(0.01)

    pytest.fail("provider thread lock was leaked after cancelling acquire_async")


@pytest.mark.anyio
async def test_acquire_async_cancelled_waiter_does_not_block_successor(tmp_path, monkeypatch):
    """A cancelled waiter must not prevent the next live waiter from acquiring."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._thread_locks = {}
    provider._warm_pool = {}
    provider._sandbox_infos = {}
    provider._thread_sandboxes = {}
    provider._last_activity = {}
    provider._lock = aio_mod.threading.Lock()

    async def fake_acquire_internal_async(thread_id: str | None) -> str:
        assert thread_id == "thread-successor-lock"
        await asyncio.sleep(0)
        return "sandbox-successor"

    monkeypatch.setattr(provider, "_acquire_internal_async", fake_acquire_internal_async)

    thread_id = "thread-successor-lock"
    thread_lock = provider._get_thread_lock(thread_id)
    thread_lock.acquire()

    cancelled_waiter = asyncio.create_task(provider.acquire_async(thread_id))
    await asyncio.sleep(0.05)
    cancelled_waiter.cancel()
    try:
        await cancelled_waiter
    except asyncio.CancelledError:
        pass

    live_waiter = asyncio.create_task(provider.acquire_async(thread_id))
    thread_lock.release()

    assert await asyncio.wait_for(live_waiter, timeout=1) == "sandbox-successor"

    deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < deadline:
        acquired = thread_lock.acquire(blocking=False)
        if acquired:
            thread_lock.release()
            return
        await asyncio.sleep(0.01)

    pytest.fail("provider thread lock was not released after successor acquire_async")


def test_remote_backend_create_forwards_effective_user_id(monkeypatch):
    """Provisioner mode must receive user_id so PVC subPath matches user isolation."""
    remote_mod = importlib.import_module("deerflow.community.aio_sandbox.remote_backend")
    backend = remote_mod.RemoteSandboxBackend("http://provisioner:8002")
    token = set_current_user(SimpleNamespace(id="user-7"))
    posted: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sandbox_url": "http://sandbox.local"}

    def _post(url, json, timeout):  # noqa: A002 - mirrors requests.post kwarg
        posted.update({"url": url, "json": json, "timeout": timeout})
        return _Response()

    monkeypatch.setattr(remote_mod.requests, "post", _post)

    try:
        backend.create("thread-42", "sandbox-42")
    finally:
        reset_current_user(token)

    assert posted["url"] == "http://provisioner:8002/api/sandboxes"
    assert posted["json"] == {
        "sandbox_id": "sandbox-42",
        "thread_id": "thread-42",
        "user_id": "user-7",
    }


# ── Sandbox client teardown (#2872) ──────────────────────────────────────────


def _make_provider_with_active_sandbox(tmp_path, sandbox_id: str):
    """Build a provider with one active sandbox suitable for release/destroy/shutdown tests."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._warm_pool = {}
    provider._sandbox_infos = {
        sandbox_id: aio_mod.SandboxInfo(sandbox_id=sandbox_id, sandbox_url="http://sandbox-host"),
    }
    provider._thread_sandboxes = {}
    provider._last_activity = {sandbox_id: 0.0}
    provider._shutdown_called = False
    provider._idle_checker_thread = None
    provider._backend = SimpleNamespace(destroy=MagicMock())

    sandbox = MagicMock()
    sandbox.id = sandbox_id
    sandbox.close = MagicMock()
    provider._sandboxes = {sandbox_id: sandbox}
    return provider, sandbox, aio_mod


def test_release_closes_cached_sandbox_client(tmp_path):
    """release() must close the host-side client owned by the cached AioSandbox (#2872)."""
    provider, sandbox, _ = _make_provider_with_active_sandbox(tmp_path, "sandbox-rel")

    provider.release("sandbox-rel")

    sandbox.close.assert_called_once_with()
    # And the sandbox is parked in the warm pool (container still running).
    assert "sandbox-rel" in provider._warm_pool
    assert "sandbox-rel" not in provider._sandboxes


def test_destroy_closes_cached_sandbox_client(tmp_path):
    """destroy() must close the host-side client before backend container teardown (#2872)."""
    provider, sandbox, _ = _make_provider_with_active_sandbox(tmp_path, "sandbox-destroy")
    backend_destroy = provider._backend.destroy

    provider.destroy("sandbox-destroy")

    sandbox.close.assert_called_once_with()
    backend_destroy.assert_called_once()
    assert "sandbox-destroy" not in provider._sandboxes
    assert "sandbox-destroy" not in provider._sandbox_infos


def test_shutdown_closes_all_active_sandbox_clients(tmp_path):
    """shutdown() must close every cached AioSandbox client during teardown (#2872)."""
    provider, sandbox, _ = _make_provider_with_active_sandbox(tmp_path, "sandbox-shut")

    provider.shutdown()

    sandbox.close.assert_called_once_with()
    provider._backend.destroy.assert_called_once()
    assert provider._sandboxes == {}


def test_release_swallows_close_errors(tmp_path, caplog):
    """A failure inside sandbox.close() must not break provider release()."""
    provider, sandbox, _ = _make_provider_with_active_sandbox(tmp_path, "sandbox-rel-err")
    sandbox.close.side_effect = RuntimeError("boom")

    with caplog.at_level("WARNING"):
        provider.release("sandbox-rel-err")

    assert "Error closing sandbox sandbox-rel-err during release" in caplog.text
    # Still moved to warm pool: client teardown failure must not block lifecycle.
    assert "sandbox-rel-err" in provider._warm_pool


def test_destroy_swallows_close_errors_and_still_destroys_backend(tmp_path, caplog):
    """A failure in sandbox.close() must not skip backend container destruction."""
    provider, sandbox, _ = _make_provider_with_active_sandbox(tmp_path, "sandbox-dest-err")
    sandbox.close.side_effect = RuntimeError("boom")

    with caplog.at_level("WARNING"):
        provider.destroy("sandbox-dest-err")

    assert "Error closing sandbox sandbox-dest-err during destroy" in caplog.text
    provider._backend.destroy.assert_called_once()


# ── #3474: warm pool stale-reference recovery ────────────────────────────────


def _make_provider_with_dead_sandbox(tmp_path, sandbox_id: str, thread_id: str):
    """Build a provider whose tracked sandbox simulates a dead container.

    The fake AioSandbox raises ConnectionRefusedError on every operation,
    matching what the real httpx client would do against a port that is no
    longer listening (e.g. after `docker rm -f`).
    """
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._warm_pool = {}
    provider._thread_locks = {}
    provider._shutdown_called = False
    provider._idle_checker_thread = None
    provider._config = {"replicas": 3, "idle_timeout": 600}
    provider._backend = SimpleNamespace(
        create=MagicMock(),
        destroy=MagicMock(),
        discover=MagicMock(return_value=None),
        is_alive=MagicMock(return_value=False),
    )

    dead_sandbox = MagicMock()
    dead_sandbox.id = sandbox_id
    dead_sandbox.execute_command.side_effect = ConnectionRefusedError(f"[Errno 111] Connection refused: {sandbox_id}")
    dead_sandbox.read_file.side_effect = ConnectionRefusedError(f"[Errno 111] Connection refused: {sandbox_id}")
    dead_sandbox.write_file.side_effect = ConnectionRefusedError(f"[Errno 111] Connection refused: {sandbox_id}")

    provider._sandboxes = {sandbox_id: dead_sandbox}
    provider._sandbox_infos = {sandbox_id: aio_mod.SandboxInfo(sandbox_id=sandbox_id, sandbox_url="http://dead-host:9999")}
    provider._thread_sandboxes = {thread_id: sandbox_id}
    provider._last_activity = {sandbox_id: __import__("time").time()}
    return provider, dead_sandbox, aio_mod


def test_acquire_returns_stale_sandbox_id_without_health_check_3474(tmp_path, monkeypatch):
    """#3474 bug 1 (fixed): acquire() must evict dead sandboxes via testOnBorrow.

    When the backend reports the container is dead, acquire() must evict the
    stale reference and fall through to discover/create instead of returning
    the dead id.
    """
    provider, _dead, aio_mod = _make_provider_with_dead_sandbox(tmp_path, "deadbeef", "thread-3474")

    new_info = aio_mod.SandboxInfo(sandbox_id="fresh-sandbox", sandbox_url="http://fresh:8080")
    provider._backend.create.return_value = new_info
    monkeypatch.setattr(aio_mod, "wait_for_sandbox_ready", lambda *a, **kw: True)

    returned_id = provider.acquire("thread-3474")

    assert returned_id != "deadbeef", "acquire must not return a dead sandbox id"
    assert "deadbeef" not in provider._sandboxes, "dead sandbox must be evicted from _sandboxes"
    assert "deadbeef" not in provider._sandbox_infos, "dead sandbox must be evicted from _sandbox_infos"


def test_execute_command_raises_sandbox_connection_error_3474(tmp_path):
    """#3474 bug 2 (fixed): AioSandbox.execute_command must raise SandboxConnectionError.

    Connection failures (ConnectionRefusedError, OSError) are now propagated
    as SandboxConnectionError instead of being swallowed into an error string.
    This lets the provider and tool layer detect unreachable sandboxes.
    """
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox")
    exc_mod = importlib.import_module("deerflow.sandbox.exceptions")
    provider, _dead, _ = _make_provider_with_dead_sandbox(tmp_path, "deadbeef", "thread-3474")

    real_sandbox = aio_mod.AioSandbox.__new__(aio_mod.AioSandbox)
    real_sandbox._id = "deadbeef"
    real_sandbox._base_url = "http://dead-host:9999"
    real_sandbox._home_dir = "/tmp"
    real_sandbox._lock = aio_mod.threading.Lock()
    real_sandbox._closed = False
    real_sandbox._client = MagicMock()
    real_sandbox._client.shell.exec_command.side_effect = ConnectionRefusedError("[Errno 111] Connection refused")

    with pytest.raises(exc_mod.SandboxConnectionError) as exc_info:
        real_sandbox.execute_command("ls")

    assert "deadbeef" in str(exc_info.value)
    assert exc_info.value.sandbox_id == "deadbeef"


def test_acquire_evicts_dead_sandbox_and_creates_replacement_3474(tmp_path, monkeypatch):
    """#3474 bug 3 (fixed): acquire() must evict dead sandboxes and create replacements.

    Once the container is dead, acquire() must detect it via testOnBorrow,
    evict the stale reference, and fall through to discover/create a new one.
    """
    provider, _dead, aio_mod = _make_provider_with_dead_sandbox(tmp_path, "deadbeef", "thread-3474")

    new_info = aio_mod.SandboxInfo(sandbox_id="fresh-sandbox", sandbox_url="http://fresh:8080")
    provider._backend.create.return_value = new_info
    monkeypatch.setattr(aio_mod, "wait_for_sandbox_ready", lambda *a, **kw: True)

    returned_id = provider.acquire("thread-3474")

    assert returned_id != "deadbeef", "acquire must not return the dead sandbox"
    assert "deadbeef" not in provider._sandboxes, "dead sandbox must be evicted"
    provider._backend.create.assert_called()


def test_is_sandbox_alive_assumes_alive_on_backend_error_3474(tmp_path):
    """_is_sandbox_alive must assume alive when the backend itself raises.

    If Docker daemon is temporarily unreachable, evicting a sandbox that may
    still be running would create an orphan container (#3474).
    """
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._sandbox_infos = {"sb-1": aio_mod.SandboxInfo(sandbox_id="sb-1", sandbox_url="http://host:8080")}
    provider._backend = SimpleNamespace(is_alive=MagicMock(side_effect=RuntimeError("Docker daemon not responding")))

    assert provider._is_sandbox_alive("sb-1") is True


def test_is_sandbox_alive_returns_false_when_info_missing_3474(tmp_path):
    """_is_sandbox_alive must return False when sandbox_infos has no entry."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._sandbox_infos = {}
    provider._backend = SimpleNamespace(is_alive=MagicMock(return_value=True))

    assert provider._is_sandbox_alive("nonexistent") is False
    provider._backend.is_alive.assert_not_called()


def test_evict_dead_sandbox_handles_none_sandbox_3474(tmp_path):
    """_evict_dead_sandbox must not crash when sandbox is already gone from _sandboxes."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._sandboxes = {}
    provider._sandbox_infos = {"gone": aio_mod.SandboxInfo(sandbox_id="gone", sandbox_url="http://gone:8080")}
    provider._last_activity = {"gone": 0}
    provider._warm_pool = {}
    provider._thread_sandboxes = {"t1": "gone"}

    provider._evict_dead_sandbox("gone")

    assert "gone" not in provider._sandbox_infos
    assert "gone" not in provider._last_activity
    assert "t1" not in provider._thread_sandboxes


def test_evict_dead_sandbox_closes_sandbox_client_3474(tmp_path):
    """_evict_dead_sandbox must call sandbox.close() to release HTTP client resources."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    mock_sandbox = MagicMock()
    provider._sandboxes = {"sb-1": mock_sandbox}
    provider._sandbox_infos = {"sb-1": aio_mod.SandboxInfo(sandbox_id="sb-1", sandbox_url="http://host:8080")}
    provider._last_activity = {"sb-1": 0}
    provider._warm_pool = {}
    provider._thread_sandboxes = {}

    provider._evict_dead_sandbox("sb-1")

    mock_sandbox.close.assert_called_once()
    assert "sb-1" not in provider._sandboxes
    assert "sb-1" not in provider._sandbox_infos


def test_reclaim_warm_pool_evicts_dead_sandbox_3474(tmp_path, monkeypatch):
    """Warm pool reclaim must evict dead containers via testOnBorrow."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._thread_locks = {}
    provider._warm_pool = {}
    provider._sandbox_infos = {}
    provider._last_activity = {}
    provider._thread_sandboxes = {}
    provider._sandboxes = {}
    provider._shutdown_called = False
    provider._idle_checker_thread = None
    provider._config = {"replicas": 3}
    info = aio_mod.SandboxInfo(sandbox_id="warm-dead", sandbox_url="http://warm-dead:8080")
    provider._warm_pool = {"warm-dead": (info, __import__("time").time())}
    provider._backend = SimpleNamespace(
        create=MagicMock(),
        destroy=MagicMock(),
        discover=MagicMock(return_value=None),
        is_alive=MagicMock(return_value=False),
    )

    result = provider._reclaim_warm_pool_sandbox("thread-warm", "warm-dead")

    assert result is None
    assert "warm-dead" not in provider._sandboxes
    assert "warm-dead" not in provider._sandbox_infos


def test_alive_sandbox_is_not_evicted_on_acquire_3474(tmp_path):
    """A live sandbox must be reused, not evicted."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._thread_locks = {}
    provider._warm_pool = {}
    provider._last_activity = {}
    provider._shutdown_called = False
    provider._idle_checker_thread = None
    provider._config = {"replicas": 3}
    live_sandbox = MagicMock()
    live_info = aio_mod.SandboxInfo(sandbox_id="alive-sb", sandbox_url="http://alive:8080")
    provider._sandboxes = {"alive-sb": live_sandbox}
    provider._sandbox_infos = {"alive-sb": live_info}
    provider._thread_sandboxes = {"thread-live": "alive-sb"}
    provider._backend = SimpleNamespace(is_alive=MagicMock(return_value=True))

    result = provider._reuse_in_process_sandbox("thread-live")

    assert result == "alive-sb"
    assert "alive-sb" in provider._sandboxes
    live_sandbox.close.assert_not_called()


def test_read_file_raises_sandbox_connection_error_3474():
    """#3474 bug 2 (fixed): read_file must raise SandboxConnectionError on connection failure."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox")
    exc_mod = importlib.import_module("deerflow.sandbox.exceptions")

    real_sandbox = aio_mod.AioSandbox.__new__(aio_mod.AioSandbox)
    real_sandbox._id = "sb-read"
    real_sandbox._base_url = "http://dead:9999"
    real_sandbox._home_dir = "/tmp"
    real_sandbox._lock = aio_mod.threading.Lock()
    real_sandbox._closed = False
    real_sandbox._client = MagicMock()
    real_sandbox._client.file.read_file.side_effect = ConnectionRefusedError("[Errno 111] Connection refused")

    with pytest.raises(exc_mod.SandboxConnectionError) as exc_info:
        real_sandbox.read_file("/some/path")

    assert exc_info.value.sandbox_id == "sb-read"


def test_list_dir_raises_sandbox_connection_error_3474():
    """#3474 bug 2 (fixed): list_dir must raise SandboxConnectionError on connection failure."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox")
    exc_mod = importlib.import_module("deerflow.sandbox.exceptions")

    real_sandbox = aio_mod.AioSandbox.__new__(aio_mod.AioSandbox)
    real_sandbox._id = "sb-list"
    real_sandbox._base_url = "http://dead:9999"
    real_sandbox._home_dir = "/tmp"
    real_sandbox._lock = aio_mod.threading.Lock()
    real_sandbox._closed = False
    real_sandbox._client = MagicMock()
    real_sandbox._client.shell.exec_command.side_effect = ConnectionRefusedError("[Errno 111] Connection refused")

    with pytest.raises(exc_mod.SandboxConnectionError) as exc_info:
        real_sandbox.list_dir("/some/dir")

    assert exc_info.value.sandbox_id == "sb-list"


def test_write_file_append_propagates_sandbox_connection_error_3474():
    """#3474 bug 2 (fixed): write_file(append=True) must propagate SandboxConnectionError from read_file."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox")
    exc_mod = importlib.import_module("deerflow.sandbox.exceptions")

    real_sandbox = aio_mod.AioSandbox.__new__(aio_mod.AioSandbox)
    real_sandbox._id = "sb-write"
    real_sandbox._base_url = "http://dead:9999"
    real_sandbox._home_dir = "/tmp"
    real_sandbox._lock = aio_mod.threading.Lock()
    real_sandbox._closed = False
    real_sandbox._client = MagicMock()
    real_sandbox._client.file.read_file.side_effect = ConnectionRefusedError("[Errno 111] Connection refused")

    with pytest.raises(exc_mod.SandboxConnectionError):
        real_sandbox.write_file("/some/path", "content", append=True)


def test_destroy_tolerates_already_gone_container_3474(tmp_path):
    """#3474 bug 3 (fixed): destroy() must not raise when the container is already gone."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._warm_pool = {}
    provider._thread_locks = {}
    provider._shutdown_called = False
    provider._idle_checker_thread = None
    provider._config = {"replicas": 3}
    mock_sandbox = MagicMock()
    info = aio_mod.SandboxInfo(sandbox_id="gone-sb", sandbox_url="http://gone:8080")
    provider._sandboxes = {"gone-sb": mock_sandbox}
    provider._sandbox_infos = {"gone-sb": info}
    provider._thread_sandboxes = {}
    provider._last_activity = {"gone-sb": 0}
    provider._backend = SimpleNamespace(destroy=MagicMock(side_effect=RuntimeError("No such container: gone-sb")))

    provider.destroy("gone-sb")

    assert "gone-sb" not in provider._sandboxes
    assert "gone-sb" not in provider._sandbox_infos
    mock_sandbox.close.assert_called_once()


def test_destroy_logs_warning_on_unexpected_error_3474(tmp_path):
    """#3474 bug 3: destroy() must log warning but not raise on unexpected backend errors."""
    aio_mod = importlib.import_module("deerflow.community.aio_sandbox.aio_sandbox_provider")
    provider = _make_provider(tmp_path)
    provider._lock = aio_mod.threading.Lock()
    provider._warm_pool = {}
    provider._thread_locks = {}
    provider._shutdown_called = False
    provider._idle_checker_thread = None
    provider._config = {"replicas": 3}
    mock_sandbox = MagicMock()
    info = aio_mod.SandboxInfo(sandbox_id="err-sb", sandbox_url="http://err:8080")
    provider._sandboxes = {"err-sb": mock_sandbox}
    provider._sandbox_infos = {"err-sb": info}
    provider._thread_sandboxes = {}
    provider._last_activity = {"err-sb": 0}
    provider._backend = SimpleNamespace(destroy=MagicMock(side_effect=RuntimeError("Permission denied")))

    provider.destroy("err-sb")

    assert "err-sb" not in provider._sandboxes
    mock_sandbox.close.assert_called_once()
