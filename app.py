import os
import shutil
import stat
import subprocess
import json
import time
import sqlite3
import platform

from flask import Flask, render_template, request, redirect, session
from git import Repo
from flask import request

app = Flask(__name__)
app.secret_key = "super_secret_key"

# -------- CONFIG --------
CLONE_DIR = "cloned_repo"
IMAGE_NAME = "intelligent_app_image"
MAIN_CONTAINER = "intelligent_app_main"
BACKUP_IMAGE = "intelligent_backup_image"
STATE_FILE = "last_state.json"
HISTORY_FILE = "deployment_history.json"
DB_FILE = "database.db"


# ---------------- DATABASE ----------------
def get_db():
    return sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT
    )
    """)

    cursor.execute("SELECT * FROM users WHERE username='vineet'")
    admin = cursor.fetchone()

    if not admin:
        cursor.execute("""
        INSERT INTO users(username,password,role)
        VALUES('vineet','admin123','superadmin')
        """)

    conn.commit()
    conn.close()


init_db()


# ---------------- AUTH ----------------
def current_user():
    return session.get("username")


def current_role():
    return session.get("role")


def login_required():
    return "username" in session


# ---------------- UTILITIES ----------------
def remove_readonly(func, path, _):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def run_cmd(cmd, cwd=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

def is_docker_running():
    result = run_cmd(["docker", "info"])
    return result.returncode == 0

def stop_container(name):
    run_cmd(["docker", "rm", "-f", name])


def safe_delete_clone():
    if os.path.exists(CLONE_DIR):
        shutil.rmtree(CLONE_DIR, onerror=remove_readonly)


# ---------------- NEW: DYNAMIC PORT ----------------
def get_next_port():
    base = 5001

    while True:
        result = run_cmd(["docker", "ps", "--format", "{{.Ports}}"])
        ports = result.stdout

        if str(base) not in ports:
            return base

        base += 1


# ---------------- HISTORY ----------------
def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except:
        return []


def save_history(entry):
    history = load_history()
    entry["user"] = current_user() if current_user() else "unknown"
    history.append(entry)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=4)


# ---------------- STATE ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=4)


# ---------------- PLATFORM DETECTION ----------------
def get_docker_platform():
    system_name = platform.system()

    if system_name in ["Windows", "Darwin"]:
        return "--platform=linux/amd64"

    return ""


# ---------------- PROJECT DETECTION ----------------
def detect_project_type():
    for root, dirs, files in os.walk(CLONE_DIR):

        if "requirements.txt" in files:
            return "Python", "5001:5000"

        if "package.json" in files:
            return "Node.js", "5001:3000"

        if any(f.endswith(".html") for f in files):
            return "Static Website", "5001:80"

    return "Unsupported", None


# ---------------- DOCKERFILE ----------------
def generate_dockerfile(project_type):
    dockerfile = os.path.join(CLONE_DIR, "Dockerfile")

    if os.path.exists(dockerfile):
        return True

    platform_flag = get_docker_platform()

    if project_type == "Python":
        content = f"""FROM {platform_flag} python:3.10
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt || true
EXPOSE 5000
CMD ["sh","-c","python app.py --host=0.0.0.0 || python main.py --host=0.0.0.0 || python run.py --host=0.0.0.0"]
"""

    elif project_type == "Node.js":
        content = f"""FROM {platform_flag} node:18-bullseye
WORKDIR /app
COPY . .

RUN npm install || true

# Try building if it's a frontend app
RUN npm run build || true

# install serve globally (for frontend apps)
RUN npm install -g serve || true

EXPOSE 3000

CMD ["sh","-c","npm start -- --host 0.0.0.0 || serve -s build -l 3000 || node server.js || tail -f /dev/null"]
"""

    elif project_type == "Static Website":
        content = f"""FROM {platform_flag} nginx:alpine

# remove default nginx files
RUN rm -rf /usr/share/nginx/html/*

# copy project files
COPY . /usr/share/nginx/html

# ensure index.html exists (fallback)
RUN if [ ! -f /usr/share/nginx/html/index.html ]; then \
    find /usr/share/nginx/html -name index.html -exec cp {{}} /usr/share/nginx/html/index.html \\; ; \
    fi

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
"""

    else:
        return False

    with open(dockerfile, "w") as f:
        f.write(content)

    return True


# ---------------- DOCKER ----------------
def docker_build():
    run_cmd(["docker", "rmi", "-f", IMAGE_NAME])

    result = run_cmd(["docker", "build", "-t", IMAGE_NAME, "."], cwd=CLONE_DIR)

    if result.returncode != 0:
        result = run_cmd(["docker", "build", "-t", IMAGE_NAME, "."], cwd=CLONE_DIR)

    return result.returncode == 0, result.stderr


def backup_current_container():
    running = run_cmd(
        ["docker", "ps", "-q", "-f", f"name={MAIN_CONTAINER}"]
    ).stdout.strip()

    if running:
        run_cmd(["docker", "commit", MAIN_CONTAINER, BACKUP_IMAGE])


# 🔥 UPDATED DEPLOY (MULTI PORT)
def deploy_main(port):
    new_port = get_next_port()
    internal_port = port.split(":")[1]

    write_log(f"🚀 Deploying on port {new_port}")

    result = run_cmd([
        "docker", "run", "-d",
        "--name", f"{current_user()}_{MAIN_CONTAINER}_{new_port}",
        "-p", f"{new_port}:{internal_port}",
        IMAGE_NAME
    ])

    write_log(result.stdout)
    write_log(result.stderr)

    return f"LIVE → http://127.0.0.1:{new_port}"


def deploy_backup(port):
    exists = run_cmd(["docker", "images", "-q", BACKUP_IMAGE]).stdout.strip()

    if not exists:
        return "BLOCKED (No stable version yet)"

    new_port = get_next_port()
    internal_port = port.split(":")[1]

    write_log(f"↩️ Rolling back on port {new_port}")

    result = run_cmd([
        "docker", "run", "-d",
        "--name", f"{current_user()}_{MAIN_CONTAINER}_{new_port}",
        "-p", f"{new_port}:{internal_port}",
        BACKUP_IMAGE
    ])

    write_log(result.stdout)
    write_log(result.stderr)

    return f"ROLLBACK → http://127.0.0.1:{new_port}"


# ---------------- RISK ENGINE ----------------
def historical_risk(repo_url):
    history = load_history()

    total = 0
    failures = 0

    for h in history:
        if h.get("repo") == repo_url:
            total += 1
            if "ROLLBACK" in h.get("status", "") or "FAILED" in h.get("status", ""):
                failures += 1

    if total == 0:
        return 0

    return int((failures / total) * 40)


def calculate_risk(files, lines, repo_url):
    score = 0

    for f in files:
        f = f.lower()

        if f.endswith((".py", ".js", ".java")):
            score += 20
        if "auth" in f:
            score += 40
        if "config" in f:
            score += 30
        if "db" in f or f.endswith(".sql"):
            score += 40

    if lines > 200:
        score += 40
    elif lines > 50:
        score += 20

    score += historical_risk(repo_url)

    return score

def write_log(message):
    with open("logs.txt", "a", encoding="utf-8") as f:
        f.write(message + "\n")


# ---------------- LOGIN ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id,password,role FROM users WHERE username=?",
            (username,)
        )
        user = cursor.fetchone()
        conn.close()

        if user and user[1] == password:
            session["user_id"] = user[0]
            session["username"] = username
            session["role"] = user[2]
            return redirect("/")

        # 🔥 FIX HERE
        return render_template("login.html", error="Invalid username or password")

    return render_template("login.html")


# ---------------- REGISTER ----------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        cursor = conn.cursor()

        try:
            cursor.execute(
                "INSERT INTO users(username,password,role) VALUES(?,?,?)",
                (username, password, "user")
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return "Username already exists"

        conn.close()
        return redirect("/login")

    return render_template("register.html")


# ---------------- CREATE ADMIN ----------------
@app.route("/create-admin", methods=["GET", "POST"])
def create_admin():
    if session.get("role") != "superadmin":
        return "Access Denied"

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        cursor = conn.cursor()

        try:
            cursor.execute(
                "INSERT INTO users(username,password,role) VALUES(?,?,?)",
                (username, password, "admin")
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return "Username already exists"

        conn.close()
        return redirect("/admin-panel")

    return render_template("create_admin.html")


# ---------------- ADMIN PANEL ----------------
@app.route("/admin-panel")
def admin_panel():
    if session.get("role") not in ["admin", "superadmin"]:
        return "Access Denied"

    # USERS
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id,username,role FROM users")
    users = cursor.fetchall()
    conn.close()

    # 🔥 USE SAME LOGIC AS system-control
    result = run_cmd(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Ports}}|{{.Status}}"])

    containers = []

    for line in result.stdout.strip().split("\n"):
        if line:
            parts = line.split("|")

            # 🔥 safety fix (important)
            if len(parts) == 3:
                name, ports, status = parts
            elif len(parts) == 2:
                name, status = parts
                ports = ""
            else:
                continue

            containers.append({
                "name": name,
                "ports": ports,
                "status": status
            })

    return render_template(
        "admin_panel.html",
        users=users,
        containers=containers
    )

@app.route("/promote/<int:user_id>")
def promote_user(user_id):
    if session.get("role") != "superadmin":
        return "Access Denied"

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT username FROM users WHERE id=?", (user_id,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return redirect("/admin-panel")

    if user[0] == "vineet":
        conn.close()
        return redirect("/admin-panel")

    cursor.execute("UPDATE users SET role='admin' WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

    return redirect("/admin-panel")


@app.route("/delete-user/<int:user_id>")
def delete_user(user_id):
    if session.get("role") != "superadmin":
        return "Access Denied"

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT username FROM users WHERE id=?", (user_id,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return redirect("/admin-panel")

    if user[0] == "vineet":
        conn.close()
        return "Cannot delete superadmin"

    cursor.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

    return redirect("/admin-panel")


# ---------------- HOME ----------------
@app.route("/")
def home():
    if not login_required():
        return redirect("/login")

    return render_template("index.html", role=current_role())


# ---------------- DASHBOARD ----------------
# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():

    if not login_required():
        return redirect("/login")

    # 🔥 NEW: GET FILTER VALUES
    repo_filter = request.args.get("repo")
    user_filter = request.args.get("user")
    risk_filter = request.args.get("risk")

    history = load_history()

    # ✅ USER ISOLATION
    if current_role() == "user":
        history = [h for h in history if h.get("user") == current_user()]

    # 🔥 APPLY FILTERS
    if repo_filter:
        history = [h for h in history if repo_filter.lower() in h.get("repo", "").lower()]

    if user_filter:
        history = [h for h in history if user_filter.lower() in h.get("user", "").lower()]

    if risk_filter:
        history = [h for h in history if h.get("risk") == risk_filter]

    total = len(history)

    low = sum(1 for h in history if h.get("risk") == "LOW")
    medium = sum(1 for h in history if h.get("risk") == "MEDIUM")
    high = sum(1 for h in history if h.get("risk") == "HIGH")

    success = sum(1 for h in history if "LIVE" in h.get("status", ""))
    rollback = sum(1 for h in history if "ROLLBACK" in h.get("status", ""))
    failed = sum(1 for h in history if "FAILED" in h.get("status", ""))

    times = [h.get("time", 0) for h in history if isinstance(h.get("time"), (int, float))]
    avg_time = round(sum(times) / len(times), 2) if times else 0
    failure_rate = round((failed / total) * 100, 2) if total else 0

    repo_stats = {}

    for h in history:
        repo = h.get("repo", "")

        if repo not in repo_stats:
            repo_stats[repo] = {"total": 0, "fail": 0}

        repo_stats[repo]["total"] += 1

        if "ROLLBACK" in h.get("status", "") or "FAILED" in h.get("status", ""):
            repo_stats[repo]["fail"] += 1

    repo_ranking = []

    for repo, data in repo_stats.items():

        if data["total"] == 0:
            continue

        rate = data["fail"] / data["total"]

        if rate > 0.4:
            level = "HIGH"
        elif rate > 0:
            level = "MEDIUM"
        else:
            level = "LOW"

        repo_ranking.append({
            "repo": repo,
            "total": data["total"],
            "fail": data["fail"],
            "risk": level
        })

    # 🔥 NEW: USER ANALYTICS (ADMIN ONLY)
    user_stats = {}
    full_history = load_history()

    for h in full_history:
        user = h.get("user", "unknown")

        if user not in user_stats:
            user_stats[user] = {"total": 0, "fail": 0}

        user_stats[user]["total"] += 1

        if "FAILED" in h.get("status", "") or "ROLLBACK" in h.get("status", ""):
            user_stats[user]["fail"] += 1

    return render_template(
        "dashboard.html",
        total=total,
        low=low,
        medium=medium,
        high=high,
        success=success,
        rollback=rollback,
        failed=failed,
        avg_time=avg_time,
        times=times,
        history=history,
        failure_rate=failure_rate,
        repo_ranking=repo_ranking,

        # 🔥 NEW
        user_stats=user_stats,
        role=current_role(),

        # 🔥 FILTER VALUES (IMPORTANT)
        repo_filter=repo_filter,
        user_filter=user_filter,
        risk_filter=risk_filter
    )

# ---------------- ANALYZE ----------------
@app.route("/analyze", methods=["POST"])
def analyze():
    if not login_required():
        return redirect("/login")

    repo_url = request.form["repo_url"]
    return render_template("progress.html", repo_url=repo_url)


# ---------------- RUN ANALYSIS ----------------
@app.route("/run-analysis", methods=["POST"])
def run_analysis():
    if not login_required():
        return redirect("/login")

    start_time = time.time()
    repo_url = request.form["repo_url"]

     # 🔥 NEW: CHECK DOCKER STATUS
    if not is_docker_running():
        return render_template(
            "result.html",
            level="N/A",
            status="DOCKER NOT RUNNING",
            time=0,
            error="Please start Docker Desktop"
        )

    # 🔥 CLEAR OLD LOGS
    open("logs.txt", "w").close()
    write_log("🚀 Starting Deployment...")

    try:
        stop_container(MAIN_CONTAINER)
        safe_delete_clone()

        # 🔥 AFTER CLONE
        write_log("📥 Cloning repository...")
        Repo.clone_from(repo_url, CLONE_DIR)

        repo = Repo(CLONE_DIR)

        commits = list(repo.iter_commits())

        changed = []
        lines = 0

        if len(commits) >= 2:
            latest, prev = commits[0], commits[1]
            diff = latest.diff(prev)

            for c in diff:
                if c.a_path:
                    changed.append(c.a_path)

            raw = repo.git.diff(prev, latest)

            for l in raw.split("\n"):
                if (l.startswith("+") and not l.startswith("+++")) or \
                   (l.startswith("-") and not l.startswith("---")):
                    lines += 1

        # 🔥 AFTER RISK
        write_log("🔍 Analyzing code changes...")
        risk = calculate_risk(changed, lines, repo_url)
        level = "LOW" if risk <= 40 else "MEDIUM" if risk <= 80 else "HIGH"
        write_log(f"⚠️ Risk Level: {level}")

        project_type, port = detect_project_type()

        if project_type == "Unsupported":
            total_time = round(time.time() - start_time, 2)

            save_history({
                "user": current_user(),
                "repo": repo_url,
                "risk": "N/A",
                "status": "UNSUPPORTED PROJECT",
                "time": total_time
            })

            return render_template(
                "result.html",
                level="N/A",
                status="UNSUPPORTED PROJECT",
                time=total_time,
                error="Project type not supported"
            )

        generate_dockerfile(project_type)

        # 🔥 BEFORE DOCKER BUILD
        write_log("🐳 Building Docker image...")
        success, error = docker_build()

        # 🔥 AFTER BUILD
        if not success:
            write_log("❌ Build Failed")
            status = "BUILD FAILED"
        else:
            write_log("✅ Build Successful")

            # 🔥 BEFORE DEPLOYMENT
            if level in ["LOW", "MEDIUM"]:
                write_log("🚀 Deploying new container...")
                backup_current_container()
                status = deploy_main(port)
            else:
                write_log("↩️ Rolling back to safe version...")
                status = deploy_backup(port)

        total_time = round(time.time() - start_time, 2)

        save_history({
            "user": current_user(),
            "repo": repo_url,
            "risk": level,
            "status": status,
            "time": total_time
        })

        # 🔥 BEFORE RETURN
        write_log("🎉 Deployment Finished")

        return render_template(
            "result.html",
            level=level,
            status=status,
            time=total_time,
            error=error if not success else ""
        )

    except Exception as e:
        return render_template(
            "result.html",
            level="N/A",
            status="ERROR",
            time=0,
            error=str(e)
        )
    
# ---------------- LOGS ----------------
@app.route("/logs")
def logs():
    try:
        with open("logs.txt", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "No logs yet"
    
#--------------SYSTEM CONTROL---------
@app.route("/system-control")
def system_control():

    if current_role() not in ["admin", "superadmin"]:
        return "Access Denied"

    result = run_cmd(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Ports}}|{{.Status}}"])

    containers = []

    for line in result.stdout.strip().split("\n"):
        if line:
            name, ports, status = line.split("|")
            containers.append({
                "name": name,
                "ports": ports,
                "status": status
            })

    return render_template("system_control.html", containers=containers)

@app.route("/start-container/<name>")
def start_container_route(name):

    if current_role() not in ["admin", "superadmin"]:
        return "Access Denied"

    run_cmd(["docker", "start", name])

    return redirect(request.referrer or "/system-control")

@app.route("/stop-container/<name>")
def stop_container_route(name):

    if current_role() not in ["admin", "superadmin"]:
        return "Access Denied"

    run_cmd(["docker", "stop", name])

    return redirect(request.referrer or "/system-control")


@app.route("/remove-container/<name>")
def remove_container_route(name):

    if current_role() not in ["admin", "superadmin"]:
        return "Access Denied"

    run_cmd(["docker", "rm", "-f", name])

    return redirect(request.referrer or "/system-control")


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)