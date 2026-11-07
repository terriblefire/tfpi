"""Small LAN web page: upload a firmware zip, review, flash, see what is installed.

Run: python3 -m tfpi.web [--host 0.0.0.0] [--port 80]
"""

import argparse
import fcntl
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import config
from . import custom, diag, files, sdk, update
from .flasher import plan
from .fwimage import FirmwareError, load_release

TOKEN_CHARS = set("0123456789abcdef")
FORM_MAX = 8 * 1024 * 1024            # everything except share uploads


def secure_amiga_name(filename):
    """Keep the file's own name (spaces and case matter on the Amiga), minus
    any path and characters AmigaDOS can't use."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c >= " " and c not in ':/"')
    return name.strip()[:30] or "file"


def web_conf():
    conf = config.read_json(config.WEB_CONF, {})
    if not conf.get("secret_key"):
        conf["secret_key"] = secrets.token_hex(32)
        config.write_json(config.WEB_CONF, conf)
        os.chmod(config.WEB_CONF, 0o600)
    return conf


def save_web_conf(conf):
    config.write_json(config.WEB_CONF, conf)
    os.chmod(config.WEB_CONF, 0o600)


def flash_running():
    if not config.LOCK_FILE.exists():
        return False
    with open(config.LOCK_FILE) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def busy_reason():
    """Why no flash, update or diagnostic job may start now (one job at a time)."""
    if diag.running():
        return "A diagnostic test or its restore is running. Wait for it to finish."
    if flash_running():
        return "A flash, software update or diagnostic test is already running."
    if update.running():
        return "A software update is running."
    return None


def a314d_state():
    try:
        return subprocess.run([config.SYSTEMCTL, "is-active", config.A314_SERVICE],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def check_token(token):
    if len(token) != 32 or not set(token) <= TOKEN_CHARS:
        abort(404)
    return token


def upload_zip(token):
    folder = config.UPLOAD_DIR / check_token(token)
    zips = list(folder.glob("*.zip")) if folder.is_dir() else []
    if len(zips) != 1:
        abort(404)
    return zips[0]


def prune_uploads(max_age=24 * 3600):
    if not config.UPLOAD_DIR.is_dir():
        return
    for folder in config.UPLOAD_DIR.iterdir():
        if time.time() - folder.stat().st_mtime > max_age:
            shutil.rmtree(folder, ignore_errors=True)


def current_job():
    job = (config.JOB_DIR / "current").read_text().strip() if (config.JOB_DIR / "current").exists() else None
    return job


def job_status(job):
    result = config.read_json(config.JOB_DIR / f"{job}.json", None)
    if result is not None:
        return "done", result
    return ("running", None) if flash_running() or diag.running() else ("stopped", None)


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = web_conf()["secret_key"]
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not web_conf().get("password_hash"):
                return redirect(url_for("setup"))
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapper

    @app.before_request
    def size_limit():
        # Share uploads stream straight to disk and may be large; nothing else may.
        if request.endpoint != "files_upload" and (request.content_length or 0) > FORM_MAX:
            abort(413)

    @app.before_request
    def csrf_protect():
        session.setdefault("csrf", secrets.token_hex(16))
        if request.method == "PUT":
            sent = request.headers.get("X-CSRF")
            if not sent or not secrets.compare_digest(sent, session["csrf"]):
                abort(400, "bad or missing CSRF token, reload the page")
        elif request.method == "POST":
            sent = request.form.get("csrf") or request.headers.get("X-CSRF")
            if not sent or not secrets.compare_digest(sent, session["csrf"]):
                abort(400, "bad or missing CSRF token, reload the page")

    @app.context_processor
    def inject():
        return {"csrf": session.get("csrf", ""), "board": config.board()}

    @app.errorhandler(413)
    def too_large(_error):
        flash("That file is too large for a firmware zip (8 MB max).", "error")
        return redirect(url_for("index"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        conf = web_conf()
        if conf.get("password_hash"):
            return redirect(url_for("login"))
        if request.method == "POST":
            password = request.form.get("password", "")
            if len(password) < 6 or password != request.form.get("confirm"):
                flash("Passwords must match and be at least 6 characters.", "error")
            else:
                conf["password_hash"] = generate_password_hash(password)
                save_web_conf(conf)
                session["user"] = "admin"
                return redirect(url_for("index"))
        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        conf = web_conf()
        if not conf.get("password_hash"):
            return redirect(url_for("setup"))
        if request.method == "POST":
            if check_password_hash(conf["password_hash"], request.form.get("password", "")):
                session["user"] = "admin"
                target = request.args.get("next", "")
                return redirect(target if target.startswith("/") and not target.startswith("//")
                                else url_for("index"))
            time.sleep(1)
            flash("Wrong password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.post("/password")
    @login_required
    def change_password():
        conf = web_conf()
        if not check_password_hash(conf["password_hash"], request.form.get("current", "")):
            flash("Current password is wrong.", "error")
        elif len(request.form.get("new", "")) < 6:
            flash("New password must be at least 6 characters.", "error")
        else:
            conf["password_hash"] = generate_password_hash(request.form["new"])
            save_web_conf(conf)
            flash("Password changed.", "ok")
        return redirect(url_for("index"))

    @app.get("/")
    @login_required
    def index():
        state = config.load_state()
        stored = sorted(config.FIRMWARE_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime,
                        reverse=True) if config.FIRMWARE_DIR.is_dir() else []
        job = current_job()
        status = job_status(job)[0] if job else None
        if app.config.get("UPDATE_AUTOCHECK") and update.stale():
            background_check()
        return render_template("index.html", state=state, stored=[p.name for p in stored],
                               diag_recovery=diag.recovery(),
                               versions=config.read_json(config.VERSIONS_FILE, {}),
                               a314d=a314d_state(), job=job, job_state=status,
                               spirom=config.SPIROM_FILE.exists(), update=update.cached(),
                               verify=diag.last_verify(), verify_warning=diag.VERIFY_WARNING,
                               warning_screen=diag.WARNING_SCREEN,
                               updating=update.running())

    checking = threading.Lock()

    def background_check():
        def work():
            try:
                update.check()
            finally:
                checking.release()
        if checking.acquire(blocking=False):
            threading.Thread(target=work, daemon=True).start()

    @app.post("/update/check")
    @login_required
    def update_check():
        result = update.check()
        if result.get("error"):
            flash(f"Couldn't check for updates: {result['error']}", "error")
        elif not result.get("available"):
            flash("This Pi is up to date.", "ok")
        return redirect(url_for("index"))

    @app.post("/update/start")
    @login_required
    def update_start():
        if busy_reason():
            flash(busy_reason(), "error")
            return redirect(url_for("index"))
        config.JOB_DIR.mkdir(parents=True, exist_ok=True)
        job = "update-" + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        try:
            update.start(config.JOB_DIR, job, str(Path(__file__).resolve().parent.parent))
        except update.UpdateError as error:
            flash(f"Couldn't start the update: {error}", "error")
            return redirect(url_for("index"))
        (config.JOB_DIR / "current").write_text(job)
        prune_jobs()
        return redirect(url_for("job_page", job=job))

    @app.get("/api/status")
    @login_required
    def api_status():
        return jsonify(state=config.load_state(), a314d=a314d_state(), flashing=flash_running(),
                       diagnosing=diag.running(),
                       versions=config.read_json(config.VERSIONS_FILE, {}))

    def new_upload(source, name):
        prune_uploads()
        token = secrets.token_hex(16)
        folder = config.UPLOAD_DIR / token
        folder.mkdir(parents=True)
        target = folder / name
        if hasattr(source, "save"):
            source.save(target)
        else:
            shutil.copyfile(source, target)
        with open(target, "rb") as handle:
            os.fsync(handle.fileno())
        return token

    @app.post("/upload")
    @login_required
    def upload():
        file = request.files.get("firmware")
        name = secure_filename(file.filename) if file and file.filename else ""
        if not name.lower().endswith(".zip"):
            flash("Choose a firmware .zip file.", "error")
            return redirect(url_for("index"))
        token = new_upload(file, name)
        if sdk.is_sdk(upload_zip(token)):
            return start_custom(upload_zip(token), name)
        return redirect(url_for("review", token=token))

    def start_custom(path, name):
        try:
            session_token = custom.create(path, name)
        except (custom.CustomError, ValueError) as error:
            flash(f"{name}: {error}", "error")
            return redirect(url_for("index"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)     # the session keeps its own copy
        return redirect(url_for("custom_page", token=session_token))

    @app.post("/review/<token>/customise")
    @login_required
    def customise_release(token):
        path = upload_zip(token)
        try:
            session_token = custom.create(path, path.name)
        except (custom.CustomError, ValueError) as error:
            flash(f"{path.name}: {error}", "error")
            return redirect(url_for("review", token=token))
        return redirect(url_for("custom_page", token=session_token))

    def custom_session(token):
        try:
            return custom.Session(token)
        except custom.CustomError:
            abort(404)

    @app.get("/custom/<token>")
    @login_required
    def custom_page(token):
        session_ = custom_session(token)
        try:
            base = session_.base()
            modules = session_.modules()
            volume, root = session_.romfs()
            size, limit = session_.estimate()
        except (custom.CustomError, ValueError) as error:
            flash(f"Can't open this SDK: {error}", "error")
            return redirect(url_for("index"))
        from .spirom import walk
        tree = [(path, node) for path, node in walk(root)]
        folders = sorted({p for p, n in tree if n.is_dir()}, key=str.lower)
        added = {a["path"].lower() for a in session_.edits["add"]}
        return render_template("custom.html", token=token, base=base, modules=modules,
                               volume=volume, tree=tree, folders=folders, added=added,
                               size=size, limit=limit, changed=session_.toc_changed(),
                               edits=session_.edits, flash_bytes=custom.flash_bytes())

    def custom_action(token, action):
        session_ = custom_session(token)
        try:
            action(session_)
        except (custom.CustomError, ValueError) as error:
            flash(str(error), "error")
        return redirect(url_for("custom_page", token=token))

    def uploaded_file():
        file = request.files.get("file")
        if not file or not file.filename:
            raise custom.CustomError("choose a file")
        data = file.read()
        if not data:
            raise custom.CustomError("that file is empty")
        return secure_amiga_name(file.filename), data

    @app.post("/custom/<token>/module")
    @login_required
    def custom_add_module(token):
        return custom_action(token, lambda s: s.add_module(*uploaded_file()))

    @app.post("/custom/<token>/module/<int:index>/<what>")
    @login_required
    def custom_module(token, index, what):
        if what == "remove":
            return custom_action(token, lambda s: s.remove_module(index))
        if what in ("up", "down"):
            return custom_action(token, lambda s: s.move_module(index, -1 if what == "up" else 1))
        abort(404)

    @app.post("/custom/<token>/file")
    @login_required
    def custom_add_file(token):
        return custom_action(token, lambda s: s.add_file(request.form.get("folder", ""), *uploaded_file()))

    @app.post("/custom/<token>/remove")
    @login_required
    def custom_remove(token):
        return custom_action(token, lambda s: s.remove_path(request.form.get("path", "")))

    @app.post("/custom/<token>/build")
    @login_required
    def custom_build(token):
        session_ = custom_session(token)
        try:
            built = session_.build()
        except (custom.CustomError, sdk.SdkError, ValueError) as error:
            flash(f"Build failed: {error}", "error")
            return redirect(url_for("custom_page", token=token))
        flash(f"Built {built.name}. Review it below, then flash.", "ok")
        return redirect(url_for("review", token=new_upload(built, built.name)))

    @app.post("/stored/<name>")
    @login_required
    def review_stored(name):
        path = config.FIRMWARE_DIR / secure_filename(name)
        if not path.is_file():
            abort(404)
        return redirect(url_for("review", token=new_upload(path, path.name)))

    @app.get("/review/<token>")
    @login_required
    def review(token):
        path = upload_zip(token)
        try:
            release = load_release(path, name=path.name, board=config.board(), spirom=config.spirom())
        except FirmwareError as error:
            flash(f"{path.name} rejected: {error}", "error")
            shutil.rmtree(path.parent, ignore_errors=True)
            return redirect(url_for("index"))
        state = config.load_state()
        return render_template("review.html", token=token, release=release.summary(),
                               plan=plan(release, state), state=state, busy=busy_reason())

    @app.post("/flash/<token>")
    @login_required
    def start_flash(token):
        path = upload_zip(token)
        if busy_reason():
            flash(busy_reason(), "error")
            return redirect(url_for("index"))
        config.JOB_DIR.mkdir(parents=True, exist_ok=True)
        job = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        cmd = [sys.executable, "-m", "tfpi.flasher", "flash", str(path), "--name", path.name,
               "--result", str(config.JOB_DIR / f"{job}.json")]
        if request.form.get("force_fpga"):
            cmd.append("--force-fpga")
        if request.form.get("force_cpld"):
            cmd.append("--force-cpld")
        return start_job(job, cmd)

    def job_file(job, suffix):
        if not job.replace("-", "").isalnum():
            abort(404)
        path = config.JOB_DIR / f"{job}{suffix}"
        if not path.exists():
            abort(404)
        return path

    @app.get("/job/<job>")
    @login_required
    def job_page(job):
        job_file(job, ".log")
        return render_template("job.html", job=job, warning=diag.WARNING_POWER,
                               warning_screen=diag.WARNING_SCREEN)

    @app.get("/job/<job>/log")
    @login_required
    def job_log(job):
        path = job_file(job, ".log")
        offset = max(0, request.args.get("offset", 0, type=int))
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(256 * 1024)
        status, result = job_status(job)
        if status == "running" and current_job() != job:
            status = "stopped"
        elif status == "stopped" and time.time() - path.stat().st_mtime < 10:
            status = "running"      # just started, flasher has not taken the lock yet
        return jsonify(text=chunk.decode("utf-8", "replace"), offset=offset + len(chunk),
                       status=status, result=result)

    # --- diagnostics ---------------------------------------------------------------

    def diag_form():
        """(actions, preset ids or None, test mask or None) from the Diagnostics form."""
        what = request.form.get("action", "all")
        actions = {"board": ["board"], "timing": ["timing"], "all": ["board", "timing"]}.get(what)
        if actions is None:
            abort(400)
        chosen = sorted({int(p) for p in request.form.getlist("preset") if p.isdigit()})
        everything = {p["id"] for p in diag.presets(diag.images_status().get("info"))}
        presets = None if not chosen or set(chosen) >= everything else chosen
        bits = [int(b) for b in request.form.getlist("test") if b.isdigit() and int(b) < 32]
        mask_text = request.form.get("tests", "").strip()
        known = {t["bit"] for t in diag.tests(diag.images_status().get("info"))}
        if bits and known and set(bits) >= known:
            mask = None                                 # every listed test = all
        elif bits:
            mask = sum(1 << b for b in set(bits))
        elif mask_text:
            try:
                mask = int(mask_text, 0) & diag.ALL_TESTS
            except ValueError:
                abort(400)
        else:
            mask = None
        if mask == diag.ALL_TESTS:
            mask = None
        return what, actions, presets, mask

    def diag_runs():
        runs = []
        folder = diag.runs_dir()
        for run in sorted(folder.iterdir(), reverse=True) if folder.is_dir() else []:
            outcome = config.read_json(run / "outcome.json", None)
            if run.is_dir() and outcome:
                runs.append({"job": run.name, "outcome": outcome,
                             "zip": diag.find_zip(run) is not None})
        return runs[:diag.KEEP_RUNS]

    @app.get("/diag")
    @login_required
    def diag_page():
        status = diag.images_status()
        info = status.get("info") or {}
        job = current_job()
        running_job = job if job and job.startswith("diag") and diag.running() else None
        return render_template("diag.html", board_experimental=diag.BOARD_EXPERIMENTAL, warning=diag.WARNING, warning_power=diag.WARNING_POWER,
                               warning_screen=diag.WARNING_SCREEN,
                               status=status, presets=diag.presets(info), tests=diag.tests(info),
                               busy=busy_reason(), running_job=running_job,
                               pending=diag.pending_restore(), recovery=diag.recovery(),
                               runs=diag_runs(), names=diag.ACTION_NAMES)

    @app.post("/diag/start")
    @login_required
    def diag_confirm():
        what, actions, presets, mask = diag_form()
        status = diag.images_status()
        if not status["ok"]:
            flash(status["reason"], "error")
            return redirect(url_for("diag_page"))
        names = {p["id"]: p["name"] for p in diag.presets(status.get("info"))}
        return render_template("diag_confirm.html", warning=diag.WARNING,
                               warning_power=diag.WARNING_POWER, warning_screen=diag.WARNING_SCREEN, first_step=diag.WARNING_FIRST_STEP,
                               what=what, actions=actions, names=diag.ACTION_NAMES, presets=presets,
                               preset_names=names, mask=mask, build=status["info"].get("build"),
                               handle_max=diag.HANDLE_MAX, note_max=diag.NOTE_MAX)

    @app.post("/diag/run")
    @login_required
    def diag_run():
        what, actions, presets, mask = diag_form()
        reason = busy_reason()
        if not reason and diag.pending_restore():
            reason = "An earlier diagnostic test was not restored. Run the restore check first."
        status = diag.images_status() if not reason else None
        if status and not status["ok"]:
            reason = status["reason"]
        if reason:
            flash(reason, "error")
            return redirect(url_for("diag_page"))
        config.JOB_DIR.mkdir(parents=True, exist_ok=True)
        job = "diag-" + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        cmd = [sys.executable, "-m", "tfpi.diag", "run", "--job", job, "--actions", ",".join(actions),
               "--result", str(config.JOB_DIR / f"{job}.json")]
        if presets:
            cmd += ["--presets", ",".join(map(str, presets))]
        if mask is not None:
            cmd += ["--tests", f"0x{mask:08x}"]
        handle = diag.clean_text(request.form.get("handle"), diag.HANDLE_MAX)
        note = diag.clean_text(request.form.get("note"), diag.NOTE_MAX, lines=True)
        if handle or note:          # the job writes them to contact.txt in its run folder
            config.write_json(config.JOB_DIR / f"{job}.contact.json", {"handle": handle, "note": note})
        return start_job(job, cmd)

    def start_job(job, cmd):
        with open(config.JOB_DIR / f"{job}.log", "w") as log_file:
            # Own session: the job finishes even if the web service restarts.
            child = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True,
                                     cwd=str(Path(__file__).resolve().parent.parent))
        threading.Thread(target=child.wait, daemon=True).start()   # reap when done
        (config.JOB_DIR / "current").write_text(job)
        prune_jobs()
        return redirect(url_for("job_page", job=job))

    @app.post("/verify")
    @login_required
    def verify_start():
        reason = busy_reason()
        if not reason and diag.pending_restore():
            reason = "An earlier diagnostic test was not restored. Run the restore check on the Diagnostics tab first."
        status = diag.images_status() if not reason else None
        if status and not status["ok"]:
            reason = status["reason"]
        if reason:
            flash(reason, "error")
            return redirect(url_for("index"))
        config.JOB_DIR.mkdir(parents=True, exist_ok=True)
        job = "verify-" + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        return start_job(job, [sys.executable, "-m", "tfpi.diag", "verify", "--job", job,
                               "--result", str(config.JOB_DIR / f"{job}.json")])

    @app.post("/diag/recover")
    @login_required
    def diag_recover():
        if busy_reason() or not diag.pending_restore():
            flash(busy_reason() or "Nothing to restore.", "error")
            return redirect(url_for("diag_page"))
        config.JOB_DIR.mkdir(parents=True, exist_ok=True)
        job = "diagrecover-" + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        return start_job(job, [sys.executable, "-m", "tfpi.diag", "recover", "--job", job,
                               "--result", str(config.JOB_DIR / f"{job}.json")])

    @app.post("/diag/forget")
    @login_required
    def diag_forget():
        if diag.running():
            flash(busy_reason(), "error")
        else:
            diag.forget()
            flash("Cleared. Power-cycle the Amiga before testing again if you haven't.", "ok")
        return redirect(url_for("diag_page"))

    @app.post("/diag/recovery/dismiss")
    @login_required
    def diag_dismiss():
        diag.dismiss_recovery()
        return redirect(request.form.get("back") if request.form.get("back") in ("/", "/diag")
                        else url_for("diag_page"))

    @app.post("/diag/abort/<job>")
    @login_required
    def diag_abort(job):
        record = diag.current() or {}
        if record.get("job") == job and diag.running() and record.get("pid"):
            try:
                os.kill(int(record["pid"]), signal.SIGTERM)
                flash("Abort sent. The machine is being restored; wait for it to finish.", "ok")
            except (OSError, ValueError):
                flash("Couldn't signal the test.", "error")
        else:
            flash("That test isn't running.", "error")
        return redirect(url_for("job_page", job=job))

    def diag_regions():
        from tfdiag import amiga
        return amiga.REGIONS

    def diag_run_dir(job):
        if not job.replace("-", "").isalnum():
            abort(404)
        folder = diag.runs_dir() / job
        if not folder.is_dir():
            abort(404)
        return folder

    @app.get("/diag/result/<job>")
    @login_required
    def diag_result(job):
        folder = diag_run_dir(job)
        outcome = config.read_json(folder / "outcome.json", None)
        found = diag.find_zip(folder)
        return render_template("diag_result.html", board_experimental=diag.BOARD_EXPERIMENTAL, job=job, outcome=outcome,
                               run=config.read_json(folder / "run.json", {}),
                               zip_name=found.name if found else None,
                               contents=diag.zip_contents(found) if found else [],
                               names=diag.ACTION_NAMES, running=diag.running() and
                               (diag.current() or {}).get("job") == job,
                               report=diag.viewable(folder, "report.html") is not None,
                               summary=diag.page_summary(folder, diag.images_status().get("info")),
                               region_titles={k: v[0] for k, v in diag_regions().items()},
                               waves=diag.wave_svgs(folder),
                               contact=diag.contact(folder), handle_max=diag.HANDLE_MAX,
                               note_max=diag.NOTE_MAX, what_to_do=diag.WHAT_TO_DO,
                               done_message=diag.DONE_MESSAGE)

    @app.post("/diag/result/<job>/contact")
    @login_required
    def diag_contact(job):
        folder = diag_run_dir(job)
        if diag.running() and (diag.current() or {}).get("job") == job:
            flash("This test is still running.", "error")
            return redirect(url_for("diag_result", job=job))
        try:
            diag.write_contact(folder, request.form.get("handle"), request.form.get("note"))
            diag.rebuild_zip(folder)
            flash("Saved in the zip (contact.txt). It's only sent if you send the zip.", "ok")
        except OSError as error:
            flash(f"Couldn't update the zip: {error}", "error")
        return redirect(url_for("diag_result", job=job))

    # The report runs its own inline script (the board view). It is served as its own
    # document under a CSP sandbox: scripts run, but in an opaque origin, so the report
    # can't reach this page's session or the rest of the app, framed or in a new tab.
    REPORT_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
                  "style-src 'unsafe-inline'; img-src data:; font-src data:")

    @app.get("/diag/result/<job>/file/<path:name>")
    @login_required
    def diag_file(job, name):
        path = diag.viewable(diag_run_dir(job), name)
        if path is None:
            abort(404)
        reply = send_file(path, mimetype=diag.VIEWABLE[path.suffix.lower()], max_age=0)
        reply.headers["Content-Security-Policy"] = REPORT_CSP
        reply.headers["X-Content-Type-Options"] = "nosniff"
        return reply

    @app.get("/diag/result/<job>/download")
    @login_required
    def diag_download(job):
        found = diag.find_zip(diag_run_dir(job))
        if found is None:
            abort(404)
        return send_file(found, as_attachment=True, download_name=found.name, max_age=0)

    # --- the a314fs share -----------------------------------------------------------

    def the_share():
        found = files.share()
        if found is None:
            flash("The a314fs share isn't set up on this Pi (no /etc/opt/a314/a314fs.conf "
                  "or its folder is missing).", "error")
        return found

    def back_to(folder):
        return redirect(url_for("files_page", path=folder or None))

    @app.get("/files")
    @login_required
    def files_page():
        found = the_share()
        if found is None:
            return redirect(url_for("index"))
        folder = request.args.get("path", "")
        try:
            directory = files.resolve(found, folder)
            if not directory.is_dir():
                raise files.FilesError(f"{folder} is not a folder")
            entries = files.listing(directory)
        except (files.FilesError, OSError) as error:
            flash(str(error), "error")
            return back_to("")
        folder = files.relative(found, directory)
        crumbs, so_far = [], []
        for part in folder.split("/") if folder else []:
            so_far.append(part)
            crumbs.append((part, "/".join(so_far)))
        return render_template("files.html", share=found, folder=folder, crumbs=crumbs,
                               entries=entries, free=files.free_bytes(found),
                               name_max=files.AMIGA_NAME_MAX)

    @app.get("/files/download")
    @login_required
    def files_download():
        found = the_share()
        if found is None:
            abort(404)
        try:
            path = files.resolve(found, request.args.get("path", ""))
        except files.FilesError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name, max_age=0)

    @app.put("/files/upload")
    @login_required
    def files_upload():
        found = files.share()
        if found is None:
            return jsonify(error="the a314fs share isn't set up on this Pi"), 404
        try:
            saved = files.save(found, request.args.get("dir", ""), request.args.get("name", ""),
                               request.stream, request.content_length,
                               overwrite=request.args.get("overwrite") == "1")
        except FileExistsError as error:
            return jsonify(error=f"{error} already exists", exists=True), 409
        except (files.FilesError, OSError) as error:
            return jsonify(error=str(error)), 400
        return jsonify(saved=saved)

    def files_action(action):
        folder = request.form.get("folder", "")
        found = the_share()
        if found is None:
            return redirect(url_for("index"))
        try:
            message = action(found)
            if message:
                flash(message, "ok")
        except (files.FilesError, OSError) as error:
            flash(str(error), "error")
        return back_to(folder)

    @app.post("/files/mkdir")
    @login_required
    def files_mkdir():
        return files_action(lambda s: files.mkdir(s, request.form.get("folder", ""),
                                                  request.form.get("name", "").strip()))

    @app.post("/files/rename")
    @login_required
    def files_rename():
        return files_action(lambda s: files.rename(s, request.form.get("path", ""),
                                                   request.form.get("name", "").strip()))

    @app.post("/files/delete")
    @login_required
    def files_delete():
        def delete(s):
            files.delete(s, request.form.get("path", ""))
            return f"Deleted {request.form.get('path', '').rsplit('/', 1)[-1]}."
        return files_action(delete)

    return app


def prune_jobs(keep=20):
    logs = sorted(config.JOB_DIR.glob("*.log"))
    for old in logs[:-keep]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    # "*" = every IPv4 and IPv6 address; browsers often try the IPv6 .local address first.
    parser.add_argument("--host", default="*")
    parser.add_argument("--port", type=int, default=80)
    args = parser.parse_args()
    app = create_app()
    app.config["UPDATE_AUTOCHECK"] = True
    try:
        from waitress import serve
        # Share uploads can be big; waitress spools bodies to $TMPDIR (the unit
        # sets /var/tmp, since /tmp is RAM on Trixie).
        serve(app, listen=f"{args.host}:{args.port}", threads=4,
              max_request_body_size=4 * 1024 ** 3)
    except ImportError:
        app.run(host="::" if args.host == "*" else args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
