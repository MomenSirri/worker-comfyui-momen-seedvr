import runpod
from runpod.serverless.utils import rp_upload
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
from io import BytesIO
import websocket
import uuid
import tempfile
import socket
import traceback
import logging
import re

from network_volume import (
    is_network_volume_debug_enabled,
    run_network_volume_diagnostics,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = int(
    os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", "250")
)
# Maximum number of API check attempts
COMFY_API_AVAILABLE_MAX_RETRIES = int(
    os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", "600")
)
# Endpoint used for health readiness checks.
# `/object_info` is API-specific and avoids false negatives when `/` returns 404.
COMFY_API_HEALTH_PATH = os.environ.get("COMFY_API_HEALTH_PATH", "/object_info")
# Websocket reconnection behaviour (can be overridden through environment variables)
# NOTE: more attempts and diagnostics improve debuggability whenever ComfyUI crashes mid-job.
#   • WEBSOCKET_RECONNECT_ATTEMPTS sets how many times we will try to reconnect.
#   • WEBSOCKET_RECONNECT_DELAY_S sets the sleep in seconds between attempts.
#
# If the respective env-vars are not supplied we fall back to sensible defaults ("5" and "3").
WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))

# Extra verbose websocket trace logs (set WEBSOCKET_TRACE=true to enable)
if os.environ.get("WEBSOCKET_TRACE", "false").lower() == "true":
    # This prints low-level frame information to stdout which is invaluable for diagnosing
    # protocol errors but can be noisy in production – therefore gated behind an env-var.
    websocket.enableTrace(True)

# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"
COMFY_RUNTIME_LOG_PATH = os.environ.get("COMFY_RUNTIME_LOG_PATH", "/comfyui/user/comfyui.log")
ENABLE_COMFY_RUNTIME_LOG_BRIDGE = (
    os.environ.get("ENABLE_COMFY_RUNTIME_LOG_BRIDGE", "true").lower() == "true"
)
SEEDVR_INPUT_FRAMES_PATTERN = re.compile(r"Input:\s*(?P<total>\d+)\s*frames", re.IGNORECASE)
SEEDVR_ENCODING_BATCH_PATTERN = re.compile(
    r"Encoding batch\s+(?P<idx>\d+)\s*/\s*(?P<total>\d+)", re.IGNORECASE
)
SEEDVR_UPSCALING_BATCH_PATTERN = re.compile(
    r"Upscaling batch\s+(?P<idx>\d+)\s*/\s*(?P<total>\d+)", re.IGNORECASE
)
SEEDVR_DECODING_BATCH_PATTERN = re.compile(
    r"Decoding batch\s+(?P<idx>\d+)\s*/\s*(?P<total>\d+)", re.IGNORECASE
)
ENHANCEMENT_TRACK_NODE_ID = (
    os.environ.get("ENHANCEMENT_TRACK_NODE_ID", "").strip() or None
)
ENHANCE_CYCLE_COMPLETE_RATIO = float(
    os.environ.get("ENHANCE_CYCLE_COMPLETE_RATIO", "0.85")
)
SAMPLER_CLASS_HINTS = (
    "ksampler",
    "samplercustom",
    "sampler",
    "euler",
    "dpm",
    "uni_pc",
)
EULER_START_PATTERN = re.compile(r"EulerSampler:\s*0%", re.IGNORECASE)
EULER_DONE_PATTERN = re.compile(r"EulerSampler:\s*100%", re.IGNORECASE)
TQDM_START_PATTERN = re.compile(r"^\s*0%\|.*\|\s*0/\d+", re.IGNORECASE)
TQDM_DONE_PATTERN = re.compile(
    r"^\s*100%\|.*\|\s*(?P<done>\d+)\s*/\s*(?P<total>\d+)",
    re.IGNORECASE,
)

# Minimum time between non-forced RunPod progress updates to avoid noisy spam
PROGRESS_UPDATE_MIN_INTERVAL_S = float(
    os.environ.get("PROGRESS_UPDATE_MIN_INTERVAL_S", "0.75")
)
# Enforce a clean state after each job is done
# see https://docs.runpod.io/docs/handler-additional-controls#refresh-worker
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"
# Refresh/terminate worker only for failed tasks (safe default: enabled for this behavior)
REFRESH_WORKER_ON_FAILURE = (
    os.environ.get("REFRESH_WORKER_ON_FAILURE", "true").lower() == "true"
)


def _failure_result(error_message, details=None):
    """
    Build a failed-task response and optionally request worker refresh.

    RunPod removes `refresh_worker` before returning output to the client,
    and recycles the worker state after this job completes.
    """
    result = {"error": str(error_message)}
    if details:
        result["details"] = details
    if REFRESH_WORKER_ON_FAILURE:
        result["refresh_worker"] = True
        print(
            "worker-comfyui - Failure detected; requesting worker refresh (REFRESH_WORKER_ON_FAILURE=true)."
        )
    return result

# ---------------------------------------------------------------------------
# Helper: quick reachability probe of ComfyUI HTTP endpoint (port 8188)
# ---------------------------------------------------------------------------


def _comfy_server_status():
    """Return a dictionary with basic reachability info for the ComfyUI HTTP server."""
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {
            # Any non-5xx response means the HTTP server is reachable.
            "reachable": resp.status_code < 500,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _attempt_websocket_reconnect(ws_url, max_attempts, delay_s, initial_error):
    """
    Attempts to reconnect to the WebSocket server after a disconnect.

    Args:
        ws_url (str): The WebSocket URL (including client_id).
        max_attempts (int): Maximum number of reconnection attempts.
        delay_s (int): Delay in seconds between attempts.
        initial_error (Exception): The error that triggered the reconnect attempt.

    Returns:
        websocket.WebSocket: The newly connected WebSocket object.

    Raises:
        websocket.WebSocketConnectionClosedException: If reconnection fails after all attempts.
    """
    print(
        f"worker-comfyui - Websocket connection closed unexpectedly: {initial_error}. Attempting to reconnect..."
    )
    last_reconnect_error = initial_error
    for attempt in range(max_attempts):
        # Log current server status before each reconnect attempt so that we can
        # see whether ComfyUI is still alive (HTTP port 8188 responding) even if
        # the websocket dropped. This is extremely useful to differentiate
        # between a network glitch and an outright ComfyUI crash/OOM-kill.
        srv_status = _comfy_server_status()
        if not srv_status["reachable"]:
            # If ComfyUI itself is down there is no point in retrying the websocket –
            # bail out immediately so the caller gets a clear "ComfyUI crashed" error.
            print(
                f"worker-comfyui - ComfyUI HTTP unreachable – aborting websocket reconnect: {srv_status.get('error', 'status '+str(srv_status.get('status_code')))}"
            )
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )

        # Otherwise we proceed with reconnect attempts while server is up
        print(
            f"worker-comfyui - Reconnect attempt {attempt + 1}/{max_attempts}... (ComfyUI HTTP reachable, status {srv_status.get('status_code')})"
        )
        try:
            # Need to create a new socket object for reconnect
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)  # Use existing ws_url
            new_ws.settimeout(1.0)
            print(f"worker-comfyui - Websocket reconnected successfully.")
            return new_ws  # Return the new connected socket
        except (
            websocket.WebSocketException,
            ConnectionRefusedError,
            socket.timeout,
            OSError,
        ) as reconn_err:
            last_reconnect_error = reconn_err
            print(
                f"worker-comfyui - Reconnect attempt {attempt + 1} failed: {reconn_err}"
            )
            if attempt < max_attempts - 1:
                print(
                    f"worker-comfyui - Waiting {delay_s} seconds before next attempt..."
                )
                time.sleep(delay_s)
            else:
                print(f"worker-comfyui - Max reconnection attempts reached.")

    # If loop completes without returning, raise an exception
    print("worker-comfyui - Failed to reconnect websocket after connection closed.")
    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_reconnect_error}"
    )



def _safe_progress_update(job, message, progress_state, force=False):
    """
    Send a RunPod progress update without letting progress reporting break the job.

    Progress updates are exposed through RunPod job status polling. We keep them as
    plain strings because that is the most widely documented/compatible format.
    """
    if not job:
        return

    now = time.time()
    last_message = progress_state.get("last_message")
    last_sent_at = progress_state.get("last_sent_at", 0.0)

    if not force:
        if message == last_message:
            return
        if now - last_sent_at < PROGRESS_UPDATE_MIN_INTERVAL_S:
            return

    try:
        runpod.serverless.progress_update(job, message)
        progress_state["last_message"] = message
        progress_state["last_sent_at"] = now
        print(f"worker-comfyui - Progress update: {message}")
    except Exception as exc:
        print(f"worker-comfyui - Failed to send progress update: {exc}")


def _emit_live_log(job, progress_state, phase, message, force=False):
    """
    Emit a structured live log line through RunPod progress updates.

    Gradio clients polling `/status` can surface these lines as a running log feed.
    """
    _safe_progress_update(
        job,
        f"[comfy-log][{phase}] {message}",
        progress_state,
        force=force,
    )


def _is_sampler_node(node_type, node_title=None):
    text = f"{node_type or ''} {node_title or ''}".strip().lower()
    if not text:
        return False

    if "saveimage" in text or "vaeencode" in text or "vaedecode" in text:
        return False

    return any(hint in text for hint in SAMPLER_CLASS_HINTS)


def _effective_enhancement_node(runtime_log_state):
    return (
        runtime_log_state.get("enhance_node_selected")
        or runtime_log_state.get("enhance_node_hint")
    )


def _is_active_enhancement_node(runtime_log_state, node_id):
    if not node_id:
        return False
    target = _effective_enhancement_node(runtime_log_state)
    if not target:
        return False
    return str(node_id) == str(target)


def _select_enhancement_node(
    job,
    progress_state,
    runtime_log_state,
    node_key,
    node_title,
    node_type,
    source="generic",
):
    selected = runtime_log_state.get("enhance_node_selected")
    if selected:
        if str(selected) == str(node_key):
            if source == "progress":
                runtime_log_state["enhance_selected_from_progress"] = True
            return True

        # Prefer nodes that emit sampler progress as the authoritative
        # enhancement node. This avoids latching onto helper sampler nodes.
        if (
            source == "progress"
            and not runtime_log_state.get("enhance_selected_from_progress", False)
        ):
            runtime_log_state["enhance_counted_current"] = False
            runtime_log_state["enhance_cycle_complete"] = False
            runtime_log_state["enhance_peak_step"] = 0
            runtime_log_state["enhance_last_step"] = None
            runtime_log_state["enhance_last_total_steps"] = None
        else:
            return False

    hint = runtime_log_state.get("enhance_node_hint")
    if hint and str(hint) != str(node_key):
        return False

    runtime_log_state["enhance_node_selected"] = str(node_key)
    runtime_log_state["enhance_node_title"] = node_title
    runtime_log_state["enhance_node_type"] = node_type
    runtime_log_state["enhance_selected_from_progress"] = source == "progress"
    _emit_live_log(
        job,
        progress_state,
        "enhance-node",
        f"selected node={node_key} type={node_type} source={source}",
        force=True,
    )
    return True


def _enhance_total_hint(runtime_log_state):
    total = runtime_log_state.get("last_frames_total")
    if isinstance(total, int) and total > 0:
        return total
    return None


def _enhance_state_values(runtime_log_state):
    node_key = (
        _effective_enhancement_node(runtime_log_state)
        or runtime_log_state.get("active_node_id")
        or "unknown"
    )
    done = int(runtime_log_state.get("enhance_samples_done") or 0)
    total_hint = _enhance_total_hint(runtime_log_state)
    if total_hint:
        done = min(done, total_hint)
    return str(node_key), done, total_hint


def _emit_enhance_state(job, progress_state, runtime_log_state, force=False):
    node_key, done, total_hint = _enhance_state_values(runtime_log_state)
    if total_hint:
        message = f"node={node_key} done={done} total={total_hint}"
    else:
        message = f"node={node_key} done={done}"
    _emit_live_log(job, progress_state, "enhance-state", message, force=force)


def _maybe_enhance_state_suffix(runtime_log_state):
    if not runtime_log_state.get("enhance_phase_initialized", False):
        return ""
    _, done, total_hint = _enhance_state_values(runtime_log_state)
    if total_hint:
        return f" [enhance_done={done}/{total_hint}]"
    if done > 0:
        return f" [enhance_done={done}]"
    return ""


def _emit_enhance_item(job, progress_state, runtime_log_state, reason):
    total_hint = _enhance_total_hint(runtime_log_state)
    runtime_log_state["enhance_samples_done"] += 1
    if total_hint:
        runtime_log_state["enhance_samples_done"] = min(
            runtime_log_state["enhance_samples_done"],
            total_hint,
        )

    done = runtime_log_state["enhance_samples_done"]
    node_key = _effective_enhancement_node(runtime_log_state) or "unknown"

    if total_hint:
        _emit_live_log(
            job,
            progress_state,
            "enhance-item",
            f"node={node_key} done={done} total={total_hint}",
            force=True,
        )
        _emit_live_log(
            job,
            progress_state,
            "enhance-sample",
            f"{done}/{total_hint}",
            force=True,
        )
    else:
        _emit_live_log(
            job,
            progress_state,
            "enhance-item",
            f"node={node_key} done={done}",
            force=True,
        )
        _emit_live_log(
            job,
            progress_state,
            "enhance-sample",
            str(done),
            force=True,
        )

    runtime_log_state["enhance_counted_current"] = True
    runtime_log_state["enhance_cycle_complete"] = True
    _emit_enhance_state(job, progress_state, runtime_log_state, force=True)
    print(
        f"worker-comfyui - Enhancement item counted ({reason}): node={node_key} done={done}"
    )


def _emit_enhance_completion_if_needed(job, progress_state, runtime_log_state, reason):
    if not runtime_log_state.get("enhance_phase_initialized", False):
        return

    node_key = _effective_enhancement_node(runtime_log_state)
    if not node_key:
        return

    total_hint = _enhance_total_hint(runtime_log_state)
    if not isinstance(total_hint, int) or total_hint <= 0:
        return

    done = int(runtime_log_state.get("enhance_samples_done") or 0)
    if done >= total_hint:
        return

    runtime_log_state["enhance_samples_done"] = total_hint
    runtime_log_state["enhance_counted_current"] = True
    runtime_log_state["enhance_cycle_complete"] = True
    runtime_log_state["enhance_peak_step"] = 0
    runtime_log_state["enhance_last_step"] = None
    runtime_log_state["enhance_last_total_steps"] = None

    _emit_live_log(
        job,
        progress_state,
        "enhance-item",
        f"node={node_key} done={total_hint} total={total_hint}",
        force=True,
    )
    _emit_live_log(
        job,
        progress_state,
        "enhance-sample",
        f"{total_hint}/{total_hint}",
        force=True,
    )
    _emit_enhance_state(job, progress_state, runtime_log_state, force=True)
    print(
        f"worker-comfyui - Enhancement completion reconciled ({reason}): node={node_key} done={total_hint}/{total_hint}"
    )


def _maybe_finalize_enhance_cycle(job, progress_state, runtime_log_state, reason):
    if runtime_log_state.get("enhance_counted_current", False):
        return

    total_steps = runtime_log_state.get("enhance_last_total_steps")
    if not isinstance(total_steps, int) or total_steps <= 0:
        return

    peak_step = int(runtime_log_state.get("enhance_peak_step") or 0)
    threshold = max(1, int(round(total_steps * max(0.0, min(1.0, ENHANCE_CYCLE_COMPLETE_RATIO)))))
    if peak_step >= threshold:
        _emit_enhance_item(job, progress_state, runtime_log_state, reason=reason)


def _init_runtime_log_state():
    state = {
        "enabled": ENABLE_COMFY_RUNTIME_LOG_BRIDGE,
        "path": COMFY_RUNTIME_LOG_PATH,
        "offset": 0,
        "last_frames_total": None,
        "last_encode": None,
        "last_upscale": None,
        "last_decode": None,
        "active_node_id": None,
        "enhance_node_hint": ENHANCEMENT_TRACK_NODE_ID,
        "enhance_node_selected": None,
        "enhance_node_type": None,
        "enhance_node_title": None,
        "enhance_selected_from_progress": False,
        "enhance_samples_done": 0,
        "enhance_counted_current": False,
        "enhance_cycle_complete": False,
        "enhance_peak_step": 0,
        "enhance_last_step": None,
        "enhance_last_total_steps": None,
        "last_enhance_total_emitted": None,
        "enhance_phase_initialized": False,
        "enhance_executed_seen": False,
    }
    if not state["enabled"]:
        return state

    try:
        state["offset"] = os.path.getsize(state["path"])
    except OSError:
        state["offset"] = 0

    return state


def _emit_seedvr_runtime_logs(job, progress_state, runtime_log_state):
    if not runtime_log_state.get("enabled"):
        return

    log_path = runtime_log_state.get("path")
    if not log_path:
        return

    try:
        current_size = os.path.getsize(log_path)
    except OSError:
        return

    if current_size < runtime_log_state["offset"]:
        runtime_log_state["offset"] = 0

    if current_size == runtime_log_state["offset"]:
        return

    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(runtime_log_state["offset"])
            chunk = fh.read()
            runtime_log_state["offset"] = fh.tell()
    except OSError:
        return

    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        frames_match = SEEDVR_INPUT_FRAMES_PATTERN.search(line)
        if frames_match:
            total = int(frames_match.group("total"))
            if runtime_log_state["last_frames_total"] != total:
                runtime_log_state["last_frames_total"] = total
                _emit_live_log(
                    job,
                    progress_state,
                    "seedvr-frames",
                    f"total={total}",
                    force=True,
                )

        encode_match = SEEDVR_ENCODING_BATCH_PATTERN.search(line)
        if encode_match:
            idx = int(encode_match.group("idx"))
            total = int(encode_match.group("total"))
            marker = (idx, total)
            if runtime_log_state["last_encode"] != marker:
                runtime_log_state["last_encode"] = marker
                _emit_live_log(
                    job,
                    progress_state,
                    "seedvr-encode",
                    f"{idx}/{total}",
                    force=True,
                )

        upscale_match = SEEDVR_UPSCALING_BATCH_PATTERN.search(line)
        if upscale_match:
            idx = int(upscale_match.group("idx"))
            total = int(upscale_match.group("total"))
            marker = (idx, total)
            if runtime_log_state["last_upscale"] != marker:
                runtime_log_state["last_upscale"] = marker
                _emit_live_log(
                    job,
                    progress_state,
                    "seedvr-upscale",
                    f"{idx}/{total}",
                    force=True,
                )

        decode_match = SEEDVR_DECODING_BATCH_PATTERN.search(line)
        if decode_match:
            idx = int(decode_match.group("idx"))
            total = int(decode_match.group("total"))
            marker = (idx, total)
            if runtime_log_state["last_decode"] != marker:
                runtime_log_state["last_decode"] = marker
                _emit_live_log(
                    job,
                    progress_state,
                    "seedvr-decode",
                    f"{idx}/{total}",
                    force=True,
                )

        active_node_id = runtime_log_state.get("active_node_id")
        if _is_active_enhancement_node(runtime_log_state, active_node_id):
            total_frames = runtime_log_state.get("last_frames_total")
            if (
                total_frames
                and runtime_log_state.get("last_enhance_total_emitted") != total_frames
            ):
                runtime_log_state["last_enhance_total_emitted"] = total_frames
                _emit_live_log(
                    job,
                    progress_state,
                    "enhance-frames",
                    f"total={total_frames}",
                    force=True,
                )

            if EULER_START_PATTERN.search(line) or TQDM_START_PATTERN.search(line):
                runtime_log_state["enhance_counted_current"] = False
                runtime_log_state["enhance_cycle_complete"] = False
                runtime_log_state["enhance_peak_step"] = 0
            elif EULER_DONE_PATTERN.search(line) or TQDM_DONE_PATTERN.search(line):
                # Fallback-only mode: if websocket 'executed' events are visible, they are
                # a more stable source for per-image enhancement counting.
                if runtime_log_state.get("enhance_executed_seen", False):
                    continue
                if not runtime_log_state.get("enhance_counted_current", False):
                    _emit_enhance_item(
                        job,
                        progress_state,
                        runtime_log_state,
                        reason="runtime-log-done",
                    )


def _get_workflow_node_display(workflow, node_id):
    """
    Resolve a ComfyUI node id to a human-friendly label using the submitted workflow.
    """
    node_key = str(node_id)
    node_info = workflow.get(node_key, {}) if isinstance(workflow, dict) else {}
    node_type = node_info.get("class_type", "Unknown")
    meta = node_info.get("_meta", {}) if isinstance(node_info, dict) else {}
    node_title = meta.get("title") or node_type
    return node_key, node_title, node_type


def validate_input(job_input):
    """
    Validates the input for the handler function.

    Args:
        job_input (dict): The input data to validate.

    Returns:
        tuple: A tuple containing the validated data and an error message, if any.
               The structure is (validated_data, error_message).
    """
    # Validate if job_input is provided
    if job_input is None:
        return None, "Please provide input"

    # Check if input is a string and try to parse it as JSON
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    # Validate 'workflow' in input
    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    # Validate 'images' in input, if provided
    images = job_input.get("images")
    if images is not None:
        if not isinstance(images, list) or not all(
            "name" in image and "image" in image for image in images
        ):
            return (
                None,
                "'images' must be a list of objects with 'name' and 'image' keys",
            )

    # Optional: API key for Comfy.org API Nodes, passed per-request
    comfy_org_api_key = job_input.get("comfy_org_api_key")

    # Return validated data and no error
    return {
        "workflow": workflow,
        "images": images,
        "comfy_org_api_key": comfy_org_api_key,
    }, None


def check_server(
    url,
    retries=COMFY_API_AVAILABLE_MAX_RETRIES,
    delay=COMFY_API_AVAILABLE_INTERVAL_MS,
):
    """
    Check if a server is reachable via HTTP GET request

    Args:
    - url (str): The URL to check
    - retries (int, optional): The number of times to attempt connecting to the server. Default is 50
    - delay (int, optional): The time in milliseconds to wait between retries. Default is 500

    Returns:
    bool: True if the server is reachable within the given number of retries, otherwise False
    """

    print(f"worker-comfyui - Checking API server at {url}...")
    for i in range(retries):
        try:
            response = requests.get(url, timeout=5)

            # Any non-5xx response indicates the HTTP server is reachable.
            # This avoids false negatives when "/" returns 404 but the API is alive.
            if response.status_code < 500:
                print(
                    f"worker-comfyui - API is reachable (status {response.status_code})"
                )
                return True
        except requests.Timeout:
            pass
        except requests.RequestException as e:
            pass

        # Wait for the specified delay before retrying
        time.sleep(delay / 1000)

    print(
        f"worker-comfyui - Failed to connect to server at {url} after {retries} attempts."
    )
    return False


def upload_images(images):
    """
    Upload a list of base64 encoded images to the ComfyUI server using the /upload/image endpoint.

    Args:
        images (list): A list of dictionaries, each containing the 'name' of the image and the 'image' as a base64 encoded string.

    Returns:
        dict: A dictionary indicating success or error.
    """
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []

    print(f"worker-comfyui - Uploading {len(images)} image(s)...")

    for image in images:
        try:
            name = image["name"]
            image_data_uri = image["image"]  # Get the full string (might have prefix)

            # --- Strip Data URI prefix if present ---
            if "," in image_data_uri:
                # Find the comma and take everything after it
                base64_data = image_data_uri.split(",", 1)[1]
            else:
                # Assume it's already pure base64
                base64_data = image_data_uri
            # --- End strip ---

            blob = base64.b64decode(base64_data)  # Decode the cleaned data

            # Prepare the form data
            files = {
                "image": (name, BytesIO(blob), "image/png"),
                "overwrite": (None, "true"),
            }

            # POST request to upload the image
            response = requests.post(
                f"http://{COMFY_HOST}/upload/image", files=files, timeout=30
            )
            response.raise_for_status()

            responses.append(f"Successfully uploaded {name}")
            print(f"worker-comfyui - Successfully uploaded {name}")

        except base64.binascii.Error as e:
            error_msg = f"Error decoding base64 for {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.Timeout:
            error_msg = f"Timeout uploading {image.get('name', 'unknown')}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.RequestException as e:
            error_msg = f"Error uploading {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except Exception as e:
            error_msg = (
                f"Unexpected error uploading {image.get('name', 'unknown')}: {e}"
            )
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)

    if upload_errors:
        print(f"worker-comfyui - image(s) upload finished with errors")
        return {
            "status": "error",
            "message": "Some images failed to upload",
            "details": upload_errors,
        }

    print(f"worker-comfyui - image(s) upload complete")
    return {
        "status": "success",
        "message": "All images uploaded successfully",
        "details": responses,
    }


def get_available_models():
    """
    Get list of available models from ComfyUI

    Returns:
        dict: Dictionary containing available models by type
    """
    try:
        response = requests.get(f"http://{COMFY_HOST}/object_info", timeout=10)
        response.raise_for_status()
        object_info = response.json()

        # Extract available checkpoints from CheckpointLoaderSimple
        available_models = {}
        if "CheckpointLoaderSimple" in object_info:
            checkpoint_info = object_info["CheckpointLoaderSimple"]
            if "input" in checkpoint_info and "required" in checkpoint_info["input"]:
                ckpt_options = checkpoint_info["input"]["required"].get("ckpt_name")
                if ckpt_options and len(ckpt_options) > 0:
                    available_models["checkpoints"] = (
                        ckpt_options[0] if isinstance(ckpt_options[0], list) else []
                    )

        return available_models
    except Exception as e:
        print(f"worker-comfyui - Warning: Could not fetch available models: {e}")
        return {}


def queue_workflow(workflow, client_id, comfy_org_api_key=None):
    """
    Queue a workflow to be processed by ComfyUI

    Args:
        workflow (dict): A dictionary containing the workflow to be processed
        client_id (str): The client ID for the websocket connection
        comfy_org_api_key (str, optional): Comfy.org API key for API Nodes

    Returns:
        dict: The JSON response from ComfyUI after processing the workflow

    Raises:
        ValueError: If the workflow validation fails with detailed error information
    """
    # Include client_id in the prompt payload
    payload = {"prompt": workflow, "client_id": client_id}

    # Optionally inject Comfy.org API key for API Nodes.
    # Precedence: per-request key (argument) overrides environment variable.
    # Note: We use our consistent naming (comfy_org_api_key) but transform to
    # ComfyUI's expected format (api_key_comfy_org) when sending.
    key_from_env = os.environ.get("COMFY_ORG_API_KEY")
    effective_key = comfy_org_api_key if comfy_org_api_key else key_from_env
    if effective_key:
        payload["extra_data"] = {"api_key_comfy_org": effective_key}
    data = json.dumps(payload).encode("utf-8")

    # Use requests for consistency and timeout
    headers = {"Content-Type": "application/json"}
    response = requests.post(
        f"http://{COMFY_HOST}/prompt", data=data, headers=headers, timeout=30
    )

    # Handle validation errors with detailed information
    if response.status_code == 400:
        print(f"worker-comfyui - ComfyUI returned 400. Response body: {response.text}")
        try:
            error_data = response.json()
            print(f"worker-comfyui - Parsed error data: {error_data}")

            # Try to extract meaningful error information
            error_message = "Workflow validation failed"
            error_details = []

            # ComfyUI seems to return different error formats, let's handle them all
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    error_message = error_info.get("message", error_message)
                    if error_info.get("type") == "prompt_outputs_failed_validation":
                        error_message = "Workflow validation failed"
                else:
                    error_message = str(error_info)

            # Check for node validation errors in the response
            if "node_errors" in error_data:
                for node_id, node_error in error_data["node_errors"].items():
                    if isinstance(node_error, dict):
                        for error_type, error_msg in node_error.items():
                            error_details.append(
                                f"Node {node_id} ({error_type}): {error_msg}"
                            )
                    else:
                        error_details.append(f"Node {node_id}: {node_error}")

            # Check if the error data itself contains validation info
            if error_data.get("type") == "prompt_outputs_failed_validation":
                error_message = error_data.get("message", "Workflow validation failed")
                # For this type of error, we need to parse the validation details from logs
                # Since ComfyUI doesn't seem to include detailed validation errors in the response
                # Let's provide a more helpful generic message
                available_models = get_available_models()
                if available_models.get("checkpoints"):
                    error_message += f"\n\nThis usually means a required model or parameter is not available."
                    error_message += f"\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                else:
                    error_message += "\n\nThis usually means a required model or parameter is not available."
                    error_message += "\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(error_message)

            # If we have specific validation errors, format them nicely
            if error_details:
                detailed_message = f"{error_message}:\n" + "\n".join(
                    f"• {detail}" for detail in error_details
                )

                # Try to provide helpful suggestions for common errors
                if any(
                    "not in list" in detail and "ckpt_name" in detail
                    for detail in error_details
                ):
                    available_models = get_available_models()
                    if available_models.get("checkpoints"):
                        detailed_message += f"\n\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                    else:
                        detailed_message += "\n\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(detailed_message)
            else:
                # Fallback to the raw response if we can't parse specific errors
                raise ValueError(f"{error_message}. Raw response: {response.text}")

        except (json.JSONDecodeError, KeyError) as e:
            # If we can't parse the error response, fall back to the raw text
            raise ValueError(
                f"ComfyUI validation failed (could not parse error response): {response.text}"
            )

    # For other HTTP errors, raise them normally
    response.raise_for_status()
    return response.json()


def get_history(prompt_id):
    """
    Retrieve the history of a given prompt using its ID

    Args:
        prompt_id (str): The ID of the prompt whose history is to be retrieved

    Returns:
        dict: The history of the prompt, containing all the processing steps and results
    """
    # Use requests for consistency and timeout
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def get_image_data(filename, subfolder, image_type):
    """
    Fetch image bytes from the ComfyUI /view endpoint.

    Args:
        filename (str): The filename of the image.
        subfolder (str): The subfolder where the image is stored.
        image_type (str): The type of the image (e.g., 'output').

    Returns:
        bytes: The raw image data, or None if an error occurs.
    """
    print(
        f"worker-comfyui - Fetching image data: type={image_type}, subfolder={subfolder}, filename={filename}"
    )
    data = {"filename": filename, "subfolder": subfolder, "type": image_type}
    url_values = urllib.parse.urlencode(data)
    try:
        # Use requests for consistency and timeout
        response = requests.get(f"http://{COMFY_HOST}/view?{url_values}", timeout=60)
        response.raise_for_status()
        print(f"worker-comfyui - Successfully fetched image data for {filename}")
        return response.content
    except requests.Timeout:
        print(f"worker-comfyui - Timeout fetching image data for {filename}")
        return None
    except requests.RequestException as e:
        print(f"worker-comfyui - Error fetching image data for {filename}: {e}")
        return None
    except Exception as e:
        print(
            f"worker-comfyui - Unexpected error fetching image data for {filename}: {e}"
        )
        return None


def handler(job):
    """
    Handles a job using ComfyUI via websockets for status and image retrieval.

    Args:
        job (dict): A dictionary containing job details and input parameters.

    Returns:
        dict: A dictionary containing either an error message or a success status with generated images.
    """
    # ---------------------------------------------------------------------------
    # Network Volume Diagnostics (opt-in via NETWORK_VOLUME_DEBUG=true)
    # ---------------------------------------------------------------------------
    if is_network_volume_debug_enabled():
        run_network_volume_diagnostics()

    job_input = job["input"]
    job_id = job["id"]

    # Make sure that the input is valid
    validated_data, error_message = validate_input(job_input)
    if error_message:
        return _failure_result(error_message)

    # Extract validated data
    workflow = validated_data["workflow"]
    input_images = validated_data.get("images")

    # Make sure that the ComfyUI HTTP API is available before proceeding
    health_path = COMFY_API_HEALTH_PATH
    if not health_path.startswith("/"):
        health_path = f"/{health_path}"

    if not check_server(
        f"http://{COMFY_HOST}{health_path}",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return _failure_result(
            f"ComfyUI server ({COMFY_HOST}) not reachable after multiple retries."
        )

    # Upload input images if they exist
    if input_images:
        upload_result = upload_images(input_images)
        if upload_result["status"] == "error":
            # Return upload errors
            return _failure_result(
                "Failed to upload one or more input images",
                details=upload_result["details"],
            )

    ws = None
    client_id = str(uuid.uuid4())
    prompt_id = None
    output_messages = []
    errors = []
    progress_state = {
        "last_message": None,
        "last_sent_at": 0.0,
        "last_queue_remaining": None,
        "last_node_id": None,
    }
    runtime_log_state = _init_runtime_log_state()

    _safe_progress_update(
        job, "Starting job and validating input...", progress_state, force=True
    )

    try:
        # Establish WebSocket connection
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        print(f"worker-comfyui - Connecting to websocket: {ws_url}")
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        ws.settimeout(1.0)
        print(f"worker-comfyui - Websocket connected")
        _safe_progress_update(job, "Connected to ComfyUI worker.", progress_state, force=True)
        _emit_live_log(job, progress_state, "ws", "connected", force=True)

        # Queue the workflow
        try:
            # Pass per-request API key if provided in input
            queued_workflow = queue_workflow(
                workflow,
                client_id,
                comfy_org_api_key=validated_data.get("comfy_org_api_key"),
            )
            prompt_id = queued_workflow.get("prompt_id")
            if not prompt_id:
                raise ValueError(
                    f"Missing 'prompt_id' in queue response: {queued_workflow}"
                )
            print(f"worker-comfyui - Queued workflow with ID: {prompt_id}")
            _emit_live_log(
                job,
                progress_state,
                "queue",
                f"queued prompt_id={prompt_id}",
                force=True,
            )
            _safe_progress_update(
                job,
                f"Workflow queued. Waiting for execution to start (prompt_id={prompt_id}).",
                progress_state,
                force=True,
            )
        except requests.RequestException as e:
            print(f"worker-comfyui - Error queuing workflow: {e}")
            raise ValueError(f"Error queuing workflow: {e}")
        except Exception as e:
            print(f"worker-comfyui - Unexpected error queuing workflow: {e}")
            # For ValueError exceptions from queue_workflow, pass through the original message
            if isinstance(e, ValueError):
                raise e
            else:
                raise ValueError(f"Unexpected error queuing workflow: {e}")

        # Wait for execution completion via WebSocket
        print(f"worker-comfyui - Waiting for workflow execution ({prompt_id})...")
        execution_done = False
        _safe_progress_update(
            job,
            "Execution started. Waiting for ComfyUI node updates...",
            progress_state,
            force=True,
        )

        while True:
            try:
                _emit_seedvr_runtime_logs(job, progress_state, runtime_log_state)
                out = ws.recv()
                if not isinstance(out, str):
                    continue

                message = json.loads(out)
                msg_type = message.get("type")

                if msg_type == "status":
                    status_data = message.get("data", {}).get("status", {})
                    queue_remaining = status_data.get("exec_info", {}).get(
                        "queue_remaining", "N/A"
                    )
                    print(
                        f"worker-comfyui - Status update: {queue_remaining} items remaining in queue"
                    )

                    if queue_remaining != progress_state.get("last_queue_remaining"):
                        progress_state["last_queue_remaining"] = queue_remaining
                        _emit_live_log(
                            job,
                            progress_state,
                            "status",
                            f"queue_remaining={queue_remaining}",
                            force=True,
                        )
                        _safe_progress_update(
                            job,
                            f"Queued in RunPod/ComfyUI. Queue remaining: {queue_remaining}",
                            progress_state,
                        )

                elif msg_type == "executing":
                    data = message.get("data", {})
                    if data.get("prompt_id") != prompt_id:
                        continue

                    current_node = data.get("node")
                    if current_node is None:
                        _maybe_finalize_enhance_cycle(
                            job,
                            progress_state,
                            runtime_log_state,
                            reason="execution-finished",
                        )
                        _emit_enhance_completion_if_needed(
                            job,
                            progress_state,
                            runtime_log_state,
                            reason="execution-finished",
                        )
                        runtime_log_state["active_node_id"] = None
                        runtime_log_state["enhance_counted_current"] = False
                        _emit_enhance_state(
                            job,
                            progress_state,
                            runtime_log_state,
                            force=True,
                        )
                        enhance_suffix = _maybe_enhance_state_suffix(runtime_log_state)
                        print(f"worker-comfyui - Execution finished for prompt {prompt_id}")
                        _emit_live_log(
                            job,
                            progress_state,
                            "execution",
                            f"finished{enhance_suffix}",
                            force=True,
                        )
                        _safe_progress_update(
                            job,
                            f"ComfyUI execution finished. Collecting outputs...{enhance_suffix}",
                            progress_state,
                            force=True,
                        )
                        execution_done = True
                        break

                    node_key, node_title, node_type = _get_workflow_node_display(workflow, current_node)
                    previous_active_node = runtime_log_state.get("active_node_id")
                    runtime_log_state["active_node_id"] = node_key

                    if (
                        previous_active_node
                        and _is_active_enhancement_node(
                            runtime_log_state, previous_active_node
                        )
                        and node_key != previous_active_node
                    ):
                        _maybe_finalize_enhance_cycle(
                            job,
                            progress_state,
                            runtime_log_state,
                            reason="node-transition",
                        )
                        _emit_enhance_completion_if_needed(
                            job,
                            progress_state,
                            runtime_log_state,
                            reason="node-transition",
                        )
                        runtime_log_state["enhance_counted_current"] = False
                        runtime_log_state["enhance_cycle_complete"] = False
                        runtime_log_state["enhance_peak_step"] = 0
                        runtime_log_state["enhance_last_step"] = None
                        runtime_log_state["enhance_last_total_steps"] = None

                    if _is_sampler_node(node_type, node_title) and _select_enhancement_node(
                        job,
                        progress_state,
                        runtime_log_state,
                        node_key,
                        node_title,
                        node_type,
                        source="executing",
                    ):
                        if not runtime_log_state.get("enhance_phase_initialized", False):
                            runtime_log_state["enhance_samples_done"] = 0
                            runtime_log_state["enhance_executed_seen"] = False
                            runtime_log_state["last_enhance_total_emitted"] = None
                            runtime_log_state["enhance_phase_initialized"] = True
                            runtime_log_state["enhance_peak_step"] = 0
                            runtime_log_state["enhance_last_step"] = None
                            runtime_log_state["enhance_last_total_steps"] = None
                        runtime_log_state["enhance_counted_current"] = False

                    if node_key != progress_state.get("last_node_id"):
                        progress_state["last_node_id"] = node_key
                        _emit_live_log(
                            job,
                            progress_state,
                            "node",
                            f"{node_key} {node_title} ({node_type})",
                            force=True,
                        )
                        _safe_progress_update(
                            job,
                            f"Running node {node_key}: {node_title} ({node_type})",
                            progress_state,
                            force=True,
                        )

                elif msg_type == "progress":
                    data = message.get("data", {})
                    if data.get("prompt_id") not in (None, prompt_id):
                        continue
                    value = data.get("value")
                    maximum = data.get("max")
                    node = data.get("node")
                    if value is not None and maximum is not None:
                        _emit_live_log(
                            job,
                            progress_state,
                            "progress",
                            f"node={node} {value}/{maximum}",
                        )

                        try:
                            value_int = int(value)
                            maximum_int = int(maximum)
                        except (TypeError, ValueError):
                            continue

                        if node is None:
                            continue

                        node_key, node_title, node_type = _get_workflow_node_display(
                            workflow, node
                        )
                        if not _is_sampler_node(node_type, node_title):
                            continue
                        if not _select_enhancement_node(
                            job,
                            progress_state,
                            runtime_log_state,
                            node_key,
                            node_title,
                            node_type,
                            source="progress",
                        ):
                            continue

                        prev_step = runtime_log_state.get("enhance_last_step")
                        prev_total = runtime_log_state.get("enhance_last_total_steps")
                        if (
                            isinstance(prev_step, int)
                            and isinstance(prev_total, int)
                            and maximum_int == prev_total
                            and value_int < prev_step
                        ):
                            if (
                                not runtime_log_state.get("enhance_counted_current", False)
                                and prev_step > 0
                            ):
                                _emit_enhance_item(
                                    job,
                                    progress_state,
                                    runtime_log_state,
                                    reason="step-reset",
                                )
                            runtime_log_state["enhance_counted_current"] = False
                            runtime_log_state["enhance_cycle_complete"] = False
                            runtime_log_state["enhance_peak_step"] = 0

                        if (
                            runtime_log_state.get("enhance_counted_current", False)
                            and value_int <= 1
                        ):
                            runtime_log_state["enhance_counted_current"] = False
                            runtime_log_state["enhance_cycle_complete"] = False
                            runtime_log_state["enhance_peak_step"] = 0

                        runtime_log_state["enhance_peak_step"] = max(
                            int(runtime_log_state.get("enhance_peak_step") or 0),
                            value_int,
                        )
                        runtime_log_state["enhance_last_step"] = value_int
                        runtime_log_state["enhance_last_total_steps"] = maximum_int

                        total_hint = _enhance_total_hint(runtime_log_state)
                        done_items = int(runtime_log_state.get("enhance_samples_done") or 0)
                        if runtime_log_state.get("enhance_counted_current", False):
                            current_item = done_items
                        else:
                            current_item = done_items + 1
                        if current_item <= 0:
                            current_item = 1

                        if total_hint:
                            current_item = min(current_item, total_hint)
                            _emit_live_log(
                                job,
                                progress_state,
                                "enhance-step",
                                f"node={node_key} item={current_item}/{total_hint} step={value_int}/{maximum_int}",
                                force=True,
                            )
                        else:
                            _emit_live_log(
                                job,
                                progress_state,
                                "enhance-step",
                                f"node={node_key} item={current_item} step={value_int}/{maximum_int}",
                                force=True,
                            )

                        if (
                            maximum_int > 0
                            and value_int >= maximum_int
                            and not runtime_log_state.get("enhance_counted_current", False)
                        ):
                            _emit_enhance_item(
                                job,
                                progress_state,
                                runtime_log_state,
                                reason="progress-max",
                            )

                        _emit_enhance_state(
                            job,
                            progress_state,
                            runtime_log_state,
                            force=True,
                        )

                elif msg_type == "execution_start":
                    data = message.get("data", {})
                    if data.get("prompt_id") != prompt_id:
                        continue
                    _emit_live_log(
                        job, progress_state, "execution", "started", force=True
                    )

                elif msg_type == "executed":
                    data = message.get("data", {})
                    if data.get("prompt_id") != prompt_id:
                        continue
                    node = data.get("node")
                    if node is not None:
                        node_key, node_title, node_type = _get_workflow_node_display(
                            workflow, node
                        )
                        if _is_sampler_node(node_type, node_title) and _select_enhancement_node(
                            job,
                            progress_state,
                            runtime_log_state,
                            node_key,
                            node_title,
                            node_type,
                            source="executed",
                        ):
                            runtime_log_state["enhance_executed_seen"] = True
                            if not runtime_log_state.get("enhance_counted_current", False):
                                _emit_enhance_item(
                                    job,
                                    progress_state,
                                    runtime_log_state,
                                    reason="executed",
                                )
                            runtime_log_state["enhance_peak_step"] = 0
                            runtime_log_state["enhance_last_step"] = None
                            runtime_log_state["enhance_last_total_steps"] = None
                        _emit_live_log(
                            job,
                            progress_state,
                            "executed",
                            f"{node_key} {node_title} ({node_type})",
                        )

                elif msg_type == "execution_cached":
                    data = message.get("data", {})
                    if data.get("prompt_id") != prompt_id:
                        continue
                    nodes = data.get("nodes", [])
                    if nodes:
                        _emit_live_log(
                            job,
                            progress_state,
                            "cache",
                            f"cached_nodes={len(nodes)}",
                            force=True,
                        )

                elif msg_type == "execution_interrupted":
                    data = message.get("data", {})
                    if data.get("prompt_id") != prompt_id:
                        continue
                    interrupted_msg = (
                        f"Execution interrupted at node {data.get('node_id')}"
                    )
                    errors.append(interrupted_msg)
                    _emit_live_log(
                        job, progress_state, "error", interrupted_msg, force=True
                    )
                    _safe_progress_update(
                        job, interrupted_msg, progress_state, force=True
                    )
                    break

                elif msg_type == "execution_error":
                    data = message.get("data", {})
                    if data.get("prompt_id") != prompt_id:
                        continue

                    error_details = (
                        f"Node Type: {data.get('node_type')}, "
                        f"Node ID: {data.get('node_id')}, "
                        f"Message: {data.get('exception_message')}"
                    )
                    print(f"worker-comfyui - Execution error received: {error_details}")
                    errors.append(f"Workflow execution error: {error_details}")
                    _emit_live_log(
                        job,
                        progress_state,
                        "error",
                        f"node={data.get('node_id')} message={data.get('exception_message')}",
                        force=True,
                    )
                    _safe_progress_update(
                        job,
                        f"Execution error at node {data.get('node_id')}: {data.get('exception_message')}",
                        progress_state,
                        force=True,
                    )
                    break

            except websocket.WebSocketTimeoutException:
                print(f"worker-comfyui - Websocket receive timed out. Still waiting...")
                _emit_seedvr_runtime_logs(job, progress_state, runtime_log_state)
                _emit_live_log(
                    job, progress_state, "ws", "receive timeout, waiting for updates"
                )
                _safe_progress_update(job, "Still running... waiting for next ComfyUI update.", progress_state)
                continue
            except websocket.WebSocketConnectionClosedException as closed_err:
                try:
                    _emit_live_log(
                        job,
                        progress_state,
                        "ws",
                        f"disconnected: {closed_err}",
                        force=True,
                    )
                    _safe_progress_update(
                        job,
                        "WebSocket disconnected. Attempting to reconnect to ComfyUI...",
                        progress_state,
                        force=True,
                    )
                    ws = _attempt_websocket_reconnect(
                        ws_url,
                        WEBSOCKET_RECONNECT_ATTEMPTS,
                        WEBSOCKET_RECONNECT_DELAY_S,
                        closed_err,
                    )

                    print(
                        "worker-comfyui - Resuming message listening after successful reconnect."
                    )
                    _emit_live_log(
                        job,
                        progress_state,
                        "ws",
                        "reconnected successfully",
                        force=True,
                    )
                    _safe_progress_update(
                        job,
                        "Reconnected to ComfyUI. Resuming execution monitoring...",
                        progress_state,
                        force=True,
                    )
                    continue
                except (
                    websocket.WebSocketConnectionClosedException
                ) as reconn_failed_err:
                    raise reconn_failed_err

            except json.JSONDecodeError:
                print(f"worker-comfyui - Received invalid JSON message via websocket.")

        if not execution_done and not errors:
            raise ValueError(
                "Workflow monitoring loop exited without confirmation of completion or error."
            )

        _emit_enhance_completion_if_needed(
            job,
            progress_state,
            runtime_log_state,
            reason="post-loop",
        )
        _emit_enhance_state(job, progress_state, runtime_log_state, force=True)
        enhance_suffix = _maybe_enhance_state_suffix(runtime_log_state)

        # Fetch history even if there were execution errors, some outputs might exist
        print(f"worker-comfyui - Fetching history for prompt {prompt_id}...")
        _safe_progress_update(
            job,
            f"Fetching execution history from ComfyUI...{enhance_suffix}",
            progress_state,
            force=True,
        )
        _emit_enhance_state(job, progress_state, runtime_log_state, force=True)
        history = get_history(prompt_id)

        if prompt_id not in history:
            error_msg = f"Prompt ID {prompt_id} not found in history after execution."
            print(f"worker-comfyui - {error_msg}")
            if not errors:
                return _failure_result(error_msg)
            else:
                errors.append(error_msg)
                return _failure_result(
                    "Job processing failed, prompt ID not found in history.",
                    details=errors,
                )

        prompt_history = history.get(prompt_id, {})
        outputs = prompt_history.get("outputs", {})

        if not outputs:
            warning_msg = f"No outputs found in history for prompt {prompt_id}."
            print(f"worker-comfyui - {warning_msg}")
            if not errors:
                errors.append(warning_msg)

        print(f"worker-comfyui - Processing {len(outputs)} output nodes...")
        _safe_progress_update(
            job,
            f"Processing output nodes and collecting images...{enhance_suffix}",
            progress_state,
            force=True,
        )
        _emit_enhance_state(job, progress_state, runtime_log_state, force=True)
        for node_id, node_output in outputs.items():
            if "images" in node_output:
                print(
                    f"worker-comfyui - Node {node_id} contains {len(node_output['images'])} image(s)"
                )
                node_key, node_title, node_type = _get_workflow_node_display(workflow, node_id)
                _safe_progress_update(
                    job,
                    f"Collecting images from node {node_key}: {node_title} ({node_type}){enhance_suffix}",
                    progress_state,
                    force=True,
                )
                _emit_enhance_state(job, progress_state, runtime_log_state, force=True)
                for image_info in node_output["images"]:
                    filename = image_info.get("filename")
                    subfolder = image_info.get("subfolder", "")
                    img_type = image_info.get("type")

                    # skip temp images
                    if img_type == "temp":
                        print(
                            f"worker-comfyui - Skipping image {filename} because type is 'temp'"
                        )
                        continue

                    if not filename:
                        warn_msg = f"Skipping image in node {node_id} due to missing filename: {image_info}"
                        print(f"worker-comfyui - {warn_msg}")
                        errors.append(warn_msg)
                        continue

                    image_bytes = get_image_data(filename, subfolder, img_type)

                    if image_bytes:
                        file_extension = os.path.splitext(filename)[1] or ".png"

                        if os.environ.get("BUCKET_ENDPOINT_URL"):
                            try:
                                with tempfile.NamedTemporaryFile(
                                    suffix=file_extension, delete=False
                                ) as temp_file:
                                    temp_file.write(image_bytes)
                                    temp_file_path = temp_file.name
                                print(
                                    f"worker-comfyui - Wrote image bytes to temporary file: {temp_file_path}"
                                )

                                print(f"worker-comfyui - Uploading {filename} to S3...")
                                s3_url = rp_upload.upload_image(job_id, temp_file_path)
                                os.remove(temp_file_path)  # Clean up temp file
                                print(
                                    f"worker-comfyui - Uploaded {filename} to S3: {s3_url}"
                                )
                                output_messages.append(s3_url)
                            except Exception as e:
                                error_msg = f"Error uploading {filename} to S3: {e}"
                                print(f"worker-comfyui - {error_msg}")
                                errors.append(error_msg)
                                if "temp_file_path" in locals() and os.path.exists(
                                    temp_file_path
                                ):
                                    try:
                                        os.remove(temp_file_path)
                                    except OSError as rm_err:
                                        print(
                                            f"worker-comfyui - Error removing temp file {temp_file_path}: {rm_err}"
                                        )
                        else:
                            # Return as base64 string
                            try:
                                base64_image = base64.b64encode(image_bytes).decode(
                                    "utf-8"
                                )
                                output_messages.append(base64_image)
                                print(f"worker-comfyui - Encoded {filename} as base64")
                            except Exception as e:
                                error_msg = f"Error encoding {filename} to base64: {e}"
                                print(f"worker-comfyui - {error_msg}")
                                errors.append(error_msg)
                    else:
                        error_msg = f"Failed to fetch image data for {filename} from /view endpoint."
                        errors.append(error_msg)

            # Check for other output types
            other_keys = [k for k in node_output.keys() if k != "images"]
            if other_keys:
                warn_msg = (
                    f"Node {node_id} produced unhandled output keys: {other_keys}."
                )
                print(f"worker-comfyui - WARNING: {warn_msg}")
                print(
                    f"worker-comfyui - --> If this output is useful, please consider opening an issue on GitHub to discuss adding support."
                )

    except websocket.WebSocketException as e:
        print(f"worker-comfyui - WebSocket Error: {e}")
        print(traceback.format_exc())
        _safe_progress_update(job, f"WebSocket communication error: {e}", progress_state, force=True)
        return _failure_result(f"WebSocket communication error: {e}")
    except requests.RequestException as e:
        print(f"worker-comfyui - HTTP Request Error: {e}")
        print(traceback.format_exc())
        _safe_progress_update(job, f"HTTP communication error with ComfyUI: {e}", progress_state, force=True)
        return _failure_result(f"HTTP communication error with ComfyUI: {e}")
    except ValueError as e:
        print(f"worker-comfyui - Value Error: {e}")
        print(traceback.format_exc())
        _safe_progress_update(job, f"Job failed: {e}", progress_state, force=True)
        return _failure_result(str(e))
    except Exception as e:
        print(f"worker-comfyui - Unexpected Handler Error: {e}")
        print(traceback.format_exc())
        _safe_progress_update(job, f"Unexpected handler error: {e}", progress_state, force=True)
        return _failure_result(f"An unexpected error occurred: {e}")
    finally:
        if ws and ws.connected:
            print(f"worker-comfyui - Closing websocket connection.")
            ws.close()

    if errors:
        print(f"worker-comfyui - Job completed with errors/warnings: {errors}")

    if not output_messages and errors:
        print(f"worker-comfyui - Job failed with no output images.")
        _safe_progress_update(job, "Job failed while collecting outputs.", progress_state, force=True)
        return _failure_result("Job processing failed", details=errors)
    elif not output_messages and not errors:
        print(
            f"worker-comfyui - Job completed successfully, but the workflow produced no images."
        )
        final_result = {"status": "success", "message": []}
        print(f"worker-comfyui - Job completed. Returning 0 image(s).")
        return final_result

    # Avoid sending progress updates after output is finalized.
    # RunPod may process late progress events out-of-order and keep request state IN_PROGRESS.
    print(f"worker-comfyui - Job completed. Returning {len(output_messages)} image(s).")
    return {"status": "success", "message": output_messages}


if __name__ == "__main__":
    print("worker-comfyui - Starting handler...")
    runpod.serverless.start({"handler": handler})
