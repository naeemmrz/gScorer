import os
import sys
import argparse
import streamlit as st
import pandas as pd
import random
import smtplib
from email.message import EmailMessage
from datetime import datetime
import time
import tempfile


# Unified image directory: all reference and final images now live in one folder.
# Legacy ref_date/ final_date directories are deprecated and no longer used.
IMAGE_DIR = "plotableimages"  # directory containing ALL images (multiple dates) per subject
# Toggle to show or hide subject IDs in the UI (redacted when False)
SHOW_SUBJECT_IDS = False
GUIDE_IMG_PATH = "gScoreGuide.png"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GLOBAL_PAIR_ORDER_PATH = os.path.join(BASE_DIR, "global_subject_pair_order.json")

# ---------------- OFFLINE MODE SUPPORT ----------------
# Allows running the app without deployed Streamlit secrets by supplying a local TOML file.

def _parse_offline_flag() -> bool:
    """Parse --offline true/false from CLI args passed after '--'.
    Accepts: true/false/1/0/yes/no/on/off (case-insensitive). Defaults to False.
    Any parsing failure -> False.
    """
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--offline", default="false")
        args, _ = parser.parse_known_args(sys.argv[1:])
        return str(args.offline).lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False

if "offline_mode" not in st.session_state:
    st.session_state.offline_mode = _parse_offline_flag()

if "offline_secrets" not in st.session_state:
    st.session_state.offline_secrets = None  # dict once loaded

def _load_offline_secrets(path: str):
    """Load secrets from a TOML file into session state.
    Supports both flat and nested tables; nested keys are flattened one level.
    """
    try:
        if not os.path.isfile(path):
            st.error("Secrets file not found at provided path.")
            return
        # Prefer tomllib (Py>=3.11), fallback to toml if installed.
        data = None
        try:
            import tomllib
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            try:
                import toml  # type: ignore
                data = toml.load(path)
            except Exception as e:
                st.error(f"Failed to parse TOML: {e}")
                return
        if not isinstance(data, dict):
            st.error("Secrets file did not yield a dict.")
            return
        flat = {}
        for k, v in data.items():
            if isinstance(v, dict):
                for sk, sv in v.items():
                    flat[sk] = sv
            else:
                flat[k] = v
        st.session_state.offline_secrets = flat
        st.success("Offline secrets loaded.")
    except Exception as e:
        st.error(f"Unexpected error loading secrets: {e}")

def get_secret(key: str, default=None):
    """Unified accessor for secrets (offline or normal)."""
    if st.session_state.get("offline_mode") and st.session_state.get("offline_secrets"):
        return st.session_state.offline_secrets.get(key, default)
    # Fall back to Streamlit secrets
    try:
        return st.secrets[key]
    except Exception:
        if default is not None:
            return default
        raise KeyError(f"Secret '{key}' not found.")

# Early prompt if offline mode enabled and secrets not yet provided
if st.session_state.offline_mode and not st.session_state.offline_secrets:
    st.info(
        "Offline mode active. Provide path to a TOML secrets file containing SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SENDER_NAME, RECIPIENT_EMAIL."
    )
    secrets_path = st.text_input("Path to local secrets TOML", value="")
    if st.button("Load secrets") and secrets_path:
        _load_offline_secrets(secrets_path)
        # trigger rerun only after successful load
        if st.session_state.offline_secrets:
            # Support both new (st.rerun) and legacy (st.experimental_rerun) APIs
            _rerun = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
            if _rerun:
                _rerun()
    # Halt rest of app until secrets available
    st.stop()
# ---------------- END OFFLINE MODE SUPPORT ----------------

# Use a temp directory for runtime files (caches/CSVs) on the server
def get_output_dir() -> str:
    base = os.path.join(tempfile.gettempdir(), "gScorer-output")
    os.makedirs(base, exist_ok=True)
    return base

# Helper: email sender used for progress and final emails
def send_email_with_attachment(subject, body, to_email, attachment_path):
    SMTP_SERVER = get_secret("SMTP_SERVER")
    SMTP_PORT = int(get_secret("SMTP_PORT"))
    SMTP_USER = get_secret("SMTP_USER")
    SMTP_PASSWORD = get_secret("SMTP_PASSWORD")
    SENDER_NAME = get_secret("SENDER_NAME")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = to_email
    msg.set_content(body)
    with open(attachment_path, "rb") as f:
        file_data = f.read()
        file_name = os.path.basename(attachment_path)
    msg.add_attachment(file_data, maintype="application", subtype="octet-stream", filename=file_name)
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        st.success("Progress Reported.")
    except Exception as e:
        st.error(f"Failed to send email: {e}")

def get_cache_path(author):
    output_dir = get_output_dir()
    return os.path.join(output_dir, f"{author}_scores_tmp.csv")

def _list_dir_images(dir_path: str):
    exts = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
    try:
        return [f for f in os.listdir(dir_path) if f.lower().endswith(exts)]
    except Exception:
        return []

def load_global_subject_pair_order():
    """Load explicit subject pair ordering from global_subject_pair_order.json.
    Expected structure: list of objects with keys subject_id, ref, final.
    Returns list of tuples (subject_id, ref, final) or None if unavailable/invalid.
    """
    try:
        import json
        with open(GLOBAL_PAIR_ORDER_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return None
        pairs = []
        for item in data:
            if not isinstance(item, dict):
                continue
            sid = item.get("subject_id")
            ref = item.get("ref")
            fin = item.get("final")
            if sid and ref and fin:
                pairs.append((str(sid), str(ref), str(fin)))
        return pairs or None
    except Exception:
        return None

def get_subject_pairs(shuffle: bool = False):
    """Return list of (subject_id, ref_filename, final_filename) using committed order if present.
    Now uses a single IMAGE_DIR containing all timestamps. Global order JSON still supplies
    explicit filename pairs; we just verify both exist inside IMAGE_DIR. If absent, we
    infer pairs by picking earliest and latest timestamps per subject.
    """
    ordered = load_global_subject_pair_order()
    all_imgs = set(_list_dir_images(IMAGE_DIR))
    if ordered:
        filtered = [tpl for tpl in ordered if tpl[1] in all_imgs and tpl[2] in all_imgs]
        if filtered:
            return filtered

    # Fallback inference: group images by subject id (segment before first underscore)
    files = [f for f in all_imgs if "_" in f]
    by_subject = {}
    for f in files:
        subject = f.split("_")[0].strip()
        by_subject.setdefault(subject, []).append(f)

    def ts_key(fname: str) -> str:
        # Use the portion after the first underscore (date_time...) for ordering.
        # Lexical order works due to YYYYMMDD_HHMMSS pattern.
        parts = fname.split("_")
        if len(parts) >= 3:
            return parts[1] + "_" + parts[2]
        return parts[1] if len(parts) > 1 else fname

    pairs = []
    for subject, flist in by_subject.items():
        if len(flist) < 2:
            continue  # need at least two timepoints
        sorted_list = sorted(flist, key=ts_key)
        ref_file = sorted_list[0]
        final_file = sorted_list[-1]
        if ref_file != final_file:
            pairs.append((subject, ref_file, final_file))

    pairs.sort(key=lambda x: x[0])
    if shuffle:
        random.shuffle(pairs)
    return pairs

# --- Session state variables ---
if "scores" not in st.session_state:
    # list of dicts: {subject_id, ref_image, final_image, score, timestamp}
    st.session_state.scores = []
if "img_idx" not in st.session_state:
    st.session_state.img_idx = 0
if "image_order" not in st.session_state:
    # list of tuples (subject_id, ref_file, final_file)
    st.session_state.image_order = []
if "reload_nonce" not in st.session_state:
    st.session_state.reload_nonce = 0
if "last_reload_idx" not in st.session_state:
    st.session_state.last_reload_idx = -1
if "scored_count" not in st.session_state:
    # Tracks how many images in the current image_order are matched by recovered scores
    st.session_state.scored_count = 0

st.title("gScorer - Skin Graft Scoring v2")



# Author selection
author_options = ["Select author...", "FI", "JH", "HS", "GA", "NA", "AAY", "Others (Please Specify)"]

# Session recovery logic
import json

def load_session_cache(author):
    cache_path = get_cache_path(author)
    if os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
            scores = df.to_dict("records")
            img_idx = len(scores)
            return {
                "scores": scores,
                "img_idx": img_idx,
                "image_order": None  # Not cached yet
            }
        except Exception:
            return None
    return None


def _realign_img_idx_to_scores():
    """Align to first unscored subject pair (by subject_id)."""
    order = st.session_state.get("image_order") or get_subject_pairs()
    scored_rows = st.session_state.get("scores", [])
    scored_ids = {r.get("subject_id") for r in scored_rows if r.get("subject_id")}
    first_unscored = None
    for i, (sid, _r, _f) in enumerate(order):
        if sid not in scored_ids:
            first_unscored = i
            break
    if first_unscored is None:
        first_unscored = len(order)
    recognized = sum(1 for sid, _r, _f in order if sid in scored_ids)
    st.session_state.scored_count = recognized
    st.session_state.img_idx = first_unscored
    mismatch = len(scored_rows) - recognized
    if mismatch > 3 and not st.session_state.get("_warned_score_mismatch"):
        st.warning(
            f"Recovered {len(scored_rows)} score rows but only {recognized} matched current subject IDs."
        )
        st.session_state._warned_score_mismatch = True

# Replace dropdown with buttons for author selection
if "author_name" not in st.session_state:
    st.subheader("Select your name to begin scoring:")

    # Ensure persistent temp selection state
    if "pending_author" not in st.session_state:
        st.session_state.pending_author = ""
    if "show_custom_author" not in st.session_state:
        st.session_state.show_custom_author = False

    # Predefined author buttons
    author_buttons = ["FI", "JH", "HS", "GA", "NA", "AAY"]
    cols = st.columns(3)
    for i, name in enumerate(author_buttons):
        if cols[i % 3].button(name, key=f"author_btn_{name}", use_container_width=True):
            st.session_state.pending_author = name
            st.session_state.show_custom_author = False
            # st.rerun() removed

    # Others flow (text input)
    if st.button("Others (Please Specify)", key="author_btn_others", use_container_width=True):
        st.session_state.show_custom_author = True

    if st.session_state.show_custom_author:
        custom_name = st.text_input("Please enter your name:", key="custom_author_input")
        if st.button("Confirm name", key="confirm_custom_author"):
            if custom_name and custom_name.strip():
                st.session_state.pending_author = custom_name.strip()
                # st.rerun() removed

    pending = st.session_state.pending_author

    if pending:
        cache_data = load_session_cache(pending)
        st.markdown(f"Selected author: **{pending}**")
        if cache_data:
            st.markdown(f"**Previous Session Recovered for {pending}.**")
            if st.button(f"Continue from previous session for {pending}", key=f"continue_prev_session_{pending}"):
                st.session_state.author_name = pending
                st.session_state.scores = cache_data["scores"]
                st.session_state.img_idx = cache_data["img_idx"]
                # Realign index to filenames in case the global order changed since the cache was written
                _realign_img_idx_to_scores()
                # Use global image order shared by all authors
                st.session_state.image_order = get_subject_pairs()
                st.session_state.last_author = pending
                st.rerun()
            if st.button("Start a new session (discard previous)", key=f"start_new_session_{pending}"):
                try:
                    os.remove(get_cache_path(pending))
                except FileNotFoundError:
                    pass
                st.session_state.author_name = pending
                st.session_state.scores = []
                st.session_state.img_idx = 0
                # Use global image order shared by all authors
                st.session_state.image_order = get_subject_pairs()
                st.session_state.last_author = pending
                st.rerun()
            st.stop()
        else:
            if st.button(f"Start scoring as {pending}", key=f"start_as_{pending}"):
                st.session_state.author_name = pending
                st.session_state.scores = []
                st.session_state.img_idx = 0
                # Use global image order shared by all authors
                st.session_state.image_order = get_subject_pairs()
                st.session_state.last_author = pending
                st.rerun()
            # Stop here until user confirms starting as pending
            st.stop()
    else:
        st.stop()
else:
    author_name = st.session_state.author_name
    st.markdown(f"**Author:** {author_name}")
    # Reset session state when author changes
    if ("last_author" not in st.session_state) or (st.session_state.last_author != author_name):
        st.session_state.scores = []
        st.session_state.img_idx = 0
        # Use global image order shared by all authors
        st.session_state.image_order = get_subject_pairs()
        st.session_state.last_author = author_name
    # Get image order after author selection
    if not st.session_state.image_order:
        st.session_state.image_order = get_subject_pairs()
    # One-time fast-forward using external tmp CSV placed next to app (e.g., HS_scores_tmp.csv)
    if not st.session_state.get("imported_external", False) and st.session_state.img_idx == 0 and not st.session_state.scores:
        # External resume disabled for pair mode
        st.session_state.imported_external = True
    image_files = st.session_state.image_order
    total_images = len(image_files)

# Always initialize image_files and total_images before use
if "author_name" not in st.session_state:
    st.stop()
image_files = st.session_state.image_order if "image_order" in st.session_state and st.session_state.image_order else []
total_images = len(image_files)


# Progress bar
_realign_img_idx_to_scores()  # Keep progress consistent on each rerun
progress_n = st.session_state.get("scored_count", st.session_state.img_idx)
st.progress(
    (progress_n / total_images) if total_images > 0 else 0,
    text=f"Progress: {progress_n}/{total_images} subject pairs scored"
)

# Optional debug / verification panel
# debug_mode temporarily disabled
# debug_mode = st.checkbox("Debug mode (show internal state)", value=False)
# if debug_mode:
#     def _norm(name: str) -> str:
#         return os.path.basename(str(name)).strip().lower()
#     order = st.session_state.get("image_order", [])
#     scored_rows = st.session_state.get("scores", [])
#     scored_set = {_norm(r.get("image", "")) for r in scored_rows if r.get("image")}
#     missing = [img for img in order if _norm(img) not in scored_set]
#     st.markdown("### Debug Summary")
#     st.write({
#         "total_images": len(order),
#         "scored_rows_len": len(scored_rows),
#         "recognized_scored_in_order": st.session_state.get("scored_count"),
#         "first_unscored_index": st.session_state.img_idx,
#         "remaining_to_score": len(missing)
#     })
#     if missing:
#         st.text(f"First 25 missing filenames: {missing[:25]}")
#     cache_path = get_cache_path(st.session_state.get("author_name", "unknown"))
#     st.caption(f"Runtime cache path: {cache_path}")
#     if os.path.exists(cache_path):
#         if st.button("Show current cache CSV (head & tail)"):
#             try:
#                 import pandas as _pd
#                 _df_dbg = _pd.read_csv(cache_path)
#                 st.dataframe(_df_dbg.head(10))
#                 st.dataframe(_df_dbg.tail(10))
#             except Exception as e:
#                 st.error(f"Failed to read cache CSV: {e}")

# Robust image display that busts cache and retries
# Single attempt by default; retry only after explicit user reload

def display_image(img_path: str, nonce: int = 0, allow_retries: bool = False) -> bool:
    file_name = os.path.basename(img_path)
    placeholder = st.empty()
    tries = 3 if allow_retries else 1
    for attempt in range(tries):
        try:
            with open(img_path, "rb") as f:
                data = f.read()
            placeholder.image(
                data,
                use_container_width=True,
            )
            return True
        except Exception:
            time.sleep(0.10)
    st.warning(f"Couldn't load image: {file_name}. Try reloading.")
    return False

def handle_score(score_value: int, subject_id: str, ref_file: str, final_file: str, total_pairs: int, author_name: str):
    """Handle a score submission for a subject pair."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state.scores.append({
        "subject_id": subject_id,
        "ref_image": ref_file,
        "final_image": final_file,
        "score": score_value,
        "timestamp": timestamp,
    })
    _realign_img_idx_to_scores()
    df_tmp = pd.DataFrame(st.session_state.scores)
    cache_path = get_cache_path(author_name)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    df_tmp.to_csv(cache_path, index=False)
    if st.session_state.img_idx % 20 == 0:
        RECIPIENT_EMAIL = get_secret("RECIPIENT_EMAIL")
        send_email_with_attachment(
            subject=f"gScorer Progress Update for {author_name}",
            body=f"{author_name} has scored {st.session_state.img_idx} out of {total_pairs} subject pairs.",
            to_email=RECIPIENT_EMAIL,
            attachment_path=cache_path
        )

# Main scoring loop
if st.session_state.img_idx < len(image_files):
    current_idx = st.session_state.img_idx
    subject_id, ref_file, final_file = image_files[current_idx]
    ref_path = os.path.join(IMAGE_DIR, ref_file)
    final_path = os.path.join(IMAGE_DIR, final_file)
    allow_retries = st.session_state.last_reload_idx == current_idx
    _ = st.session_state.get("reload_nonce", 0)
    col_ref, col_final = st.columns(2)
    with col_ref:
        if SHOW_SUBJECT_IDS:
            st.markdown(f"**{subject_id} – D1 Graft**")
        else:
            st.markdown("**D1 Graft**")
        display_image(ref_path, st.session_state.get("reload_nonce", 0), allow_retries=allow_retries)
    with col_final:
        if SHOW_SUBJECT_IDS:
            st.markdown(f"**{subject_id} – D29 Graft**")
        else:
            st.markdown("**D29 Graft**")
        display_image(final_path, st.session_state.get("reload_nonce", 0), allow_retries=allow_retries)
    st.write("Select a score for this subject pair:")
    score_labels = ["0 🍃", "1 🌱", "2 🌸", "3 🌞", "4 🔥", "5 🌋", "6 🐦‍🔥"]
    cols = st.columns(7)
    for i, col in enumerate(cols):
        col.button(
            score_labels[i],
            key=f"score_{i}_{current_idx}",
            help=f"Score {i}",
            use_container_width=True,
            on_click=handle_score,
            args=(i, subject_id, ref_file, final_file, total_images, author_name),
        )
    if st.button("Images didn't load? Reload pair", key=f"reload_pair_{current_idx}", use_container_width=True):
        st.session_state.last_reload_idx = current_idx
        st.session_state.reload_nonce = st.session_state.get("reload_nonce", 0) + 1
    st.image(GUIDE_IMG_PATH, caption="gScore Guide", use_container_width=True)
else:
    st.success("All subject pairs scored!")
    df = pd.DataFrame(st.session_state.scores)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = get_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, f"{author_name}_subject_scores_{timestamp_str}.csv")
    df.to_csv(csv_path, index=False)
    # Remove cache after completion
    cache_path = get_cache_path(author_name)
    if os.path.exists(cache_path):
        os.remove(cache_path)
    st.write(f"Scores saved to {csv_path}")
    st.dataframe(df)
    # Email the CSV file (do not show email address)
    RECIPIENT_EMAIL = get_secret("RECIPIENT_EMAIL")
    send_email_with_attachment(
        subject=f"gScorer Subject Pair Output Submitted by {author_name}",
        body=f"Final subject pair scores for {author_name} are attached.",
        to_email=RECIPIENT_EMAIL,
        attachment_path=csv_path
    )