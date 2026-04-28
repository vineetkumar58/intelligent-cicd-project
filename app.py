import os
import shutil
import stat
import subprocess
import json
import time
import re
import requests
import sqlite3
import platform

from flask import Flask, render_template, request, redirect, session, Response
from git import Repo

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


def get_clone_dir():
    return f"cloned_repo_{current_user()}"

def get_image_name():
    return f"{current_user()}_image"

def get_backup_image():
    return f"{current_user()}_backup"


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
    clone_dir = get_clone_dir()
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir, onerror=remove_readonly)


# ---------------- DYNAMIC PORT ----------------
def get_next_port():
    base = 5001

    while True:
        result = run_cmd(["docker", "ps", "--format", "{{.Ports}}"])
        ports = result.stdout.split()

        used = []

        for p in ports:
            if "->" in p:
                try:
                    host_part = p.split("->")[0]
                    port_num = host_part.split(":")[-1]
                    used.append(int(port_num))
                except:
                    pass

        if base not in used:
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
    clone_dir = get_clone_dir()

    for root, dirs, files in os.walk(clone_dir):

        if "requirements.txt" in files:
            return "Python", "5000"

        if "package.json" in files:
            return "Node.js", "3000"

        if any(f.endswith(".html") for f in files):
            return "Static Website", "80"

        if any(f.endswith(".jar") for f in files):
            return "Java", "8080"

        if any(f.endswith(".java") for f in files):
            return "Java", "8080"

        if any(f.endswith(".cpp") for f in files):
            return "C++", "0"

    return "Unsupported", None


# ---------------- DOCKERFILE ----------------
def generate_dockerfile(project_type):
    clone_dir = get_clone_dir()
    dockerfile = os.path.join(clone_dir, "Dockerfile")

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

    elif project_type == "Java":
        content = f"""FROM openjdk:17
WORKDIR /app
COPY . .

# compile all java files
RUN javac *.java || true

EXPOSE 8080

# try running jar OR main class
CMD ["sh","-c","java Main || (ls *.jar 2>/dev/null | head -n 1 | xargs -r java -jar) || tail -f /dev/null"]
"""

    elif project_type == "C++":
        content = f"""FROM gcc:latest
WORKDIR /app
COPY . .

RUN g++ *.cpp -o app || true

CMD ["sh","-c","./app || tail -f /dev/null"]
"""

    else:
        return False

    with open(dockerfile, "w") as f:
        f.write(content)

    return True


# ---------------- DOCKER BUILD ----------------
def docker_build():
    image = get_image_name()
    clone_dir = get_clone_dir()

    run_cmd(["docker", "rmi", "-f", image])

    result = run_cmd(["docker", "build", "-t", image, "."], cwd=clone_dir)

    if result.returncode != 0:
        result = run_cmd(["docker", "build", "-t", image, "."], cwd=clone_dir)

    return result.returncode == 0, result.stderr


def backup_current_container():
    backup_image = get_backup_image()

    running = run_cmd(
        ["docker", "ps", "-q", "-f", f"name={current_user()}_{MAIN_CONTAINER}"]
    ).stdout.splitlines()

    if running:
        container_id = running[-1]

        status = run_cmd(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_id]
        ).stdout.strip()

        if status == "true":
            run_cmd(["docker", "commit", container_id, backup_image])


def get_base_url():
    try:
        tunnels = requests.get("http://127.0.0.1:4040/api/tunnels").json()

        for t in tunnels["tunnels"]:
            if t["proto"] == "https":
                return t["public_url"]

        return tunnels["tunnels"][0]["public_url"]

    except:
        return "http://127.0.0.1:5000"


# ---------------- DEPLOY MAIN (LOW RISK) ----------------
def deploy_main(port, fixed_port=None):
    new_port = fixed_port if fixed_port else get_next_port()
    internal_port = port if port != "0" else None
    image = get_image_name()

    container_name = f"{current_user()}_{MAIN_CONTAINER}_{new_port}"

    write_log(f"🚀 Deploying on port {new_port}")

    run_cmd(["docker", "rm", "-f", container_name])

    result = run_cmd([
        "docker", "run", "-d",
        "--name", container_name,
        *(["-p", f"{new_port}:{internal_port}"] if internal_port else []),
        image
    ])

    write_log(result.stdout)
    write_log(result.stderr)

    if port == "0":
        return "RUNNING (C++ CLI app - no web interface)"

    base_url = get_base_url()
    return f"LIVE → {base_url}/app/{new_port}"


# ---------------- DEPLOY CANARY (MEDIUM RISK) ----------------
def deploy_canary(port):
    image = get_image_name()
    internal_port = port if port != "0" else None
    canary_port = get_next_port()
    canary_container = f"{current_user()}_{MAIN_CONTAINER}_canary_{canary_port}"

    write_log(f"🐤 Starting canary container on port {canary_port}...")

    # remove any previous canary containers for this user
    all_containers = run_cmd(
        ["docker", "ps", "-a", "--format", "{{.Names}}"]
    ).stdout.strip().split("\n")

    for c in all_containers:
        if c and f"{current_user()}_{MAIN_CONTAINER}_canary" in c:
            write_log(f"🛑 Removing old canary: {c}")
            run_cmd(["docker", "rm", "-f", c])

    result = run_cmd([
        "docker", "run", "-d",
        "--name", canary_container,
        *(["-p", f"{canary_port}:{internal_port}"] if internal_port else []),
        image
    ])

    write_log(result.stdout)
    write_log(result.stderr)

    if result.returncode != 0:
        write_log("❌ Canary deployment failed — keeping old version live")
        return "CANARY FAILED — old version still live"

    base_url = get_base_url()
    canary_url = f"{base_url}/app/{canary_port}"
    write_log(f"🐤 Canary live at: {canary_url}")
    write_log("✅ Old version still running — test canary before promoting")

    return f"CANARY → {canary_url}"


# ---------------- DEPLOY BACKUP (HIGH RISK ROLLBACK) ----------------
def deploy_backup(port):
    backup_image = get_backup_image()

    exists = run_cmd(["docker", "images", "-q", backup_image]).stdout.strip()

    if not exists:
        return "BLOCKED (No stable version yet)"

    new_port = get_next_port()
    internal_port = port if port != "0" else None

    write_log(f"↩️ Rolling back on port {new_port}")

    result = run_cmd([
        "docker", "run", "-d",
        "--name", f"{current_user()}_{MAIN_CONTAINER}_{new_port}",
        *(["-p", f"{new_port}:{internal_port}"] if internal_port else []),
        backup_image
    ])

    write_log(result.stdout)
    write_log(result.stderr)

    if port == "0":
        return "ROLLBACK → C++ CLI app (no web interface)"

    base_url = get_base_url()
    return f"ROLLBACK → {base_url}/app/{new_port}"


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


# ---------------- PROXY ----------------
@app.route("/app/<int:port>", defaults={"path": ""})
@app.route("/app/<int:port>/<path:path>")
def proxy(port, path):
    try:
        url = f"http://127.0.0.1:{port}/{path}"

        resp = requests.request(
            method=request.method,
            url=url,
            headers={key: value for key, value in request.headers if key.lower() != "host"},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True
        )

        excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]

        headers = []
        for name, value in resp.raw.headers.items():
            if name.lower() not in excluded_headers:
                if name.lower() == "location":
                    value = value.replace(
                        "http://127.0.0.1",
                        f"{get_base_url()}/app/{port}"
                    )
                headers.append((name, value))

        content_type = resp.headers.get("Content-Type", "")

        headers.append(("X-Forwarded-Proto", "https"))
        headers.append(("X-Forwarded-Host", request.host))

        if "text/html" in content_type:
            content = resp.content.decode("utf-8", errors="ignore")

            base_tag = f'<base href="/app/{port}/">'

            if "<head>" in content.lower():
                content = re.sub(r'<head>', f'<head>{base_tag}', content, count=1, flags=re.IGNORECASE)
            else:
                content = base_tag + content

            # fix absolute paths double quotes
            content = content.replace('href="/', f'href="/app/{port}/')
            content = content.replace('src="/', f'src="/app/{port}/')
            content = content.replace('action="/', f'action="/app/{port}/')

            # fix absolute paths single quotes
            content = content.replace("href='/", f"href='/app/{port}/")
            content = content.replace("src='/", f"src='/app/{port}/")
            content = content.replace("action='/", f"action='/app/{port}/")

            # fix relative paths double quotes
            content = re.sub(
                r'src="(?!http|//|/app|data:|#)([^"]+)"',
                lambda m: f'src="/app/{port}/{m.group(1)}"',
                content
            )
            content = re.sub(
                r"src='(?!http|//|/app|data:|#)([^']+)'",
                lambda m: f"src='/app/{port}/{m.group(1)}'",
                content
            )
            content = re.sub(
                r'href="(?!http|//|/app|#|mailto:|tel:|javascript:)([^"]+)"',
                lambda m: f'href="/app/{port}/{m.group(1)}"',
                content
            )
            content = re.sub(
                r"href='(?!http|//|/app|#|mailto:|tel:|javascript:)([^']+)'",
                lambda m: f"href='/app/{port}/{m.group(1)}'",
                content
            )

            # fix url() in inline styles
            content = re.sub(
                r'url\(["\']?(?!http|//|data:)([^)"\']+)["\']?\)',
                lambda m: f'url("/app/{port}/{m.group(1)}")',
                content
            )

            return Response(content, resp.status_code, headers)

        elif "text/css" in content_type:
            css_content = resp.content.decode("utf-8", errors="ignore")
            css_content = re.sub(
                r'url\(["\']?(?!http|//|data:)([^)"\']+)["\']?\)',
                lambda m: f'url("/app/{port}/{m.group(1)}")',
                css_content
            )
            return Response(css_content, resp.status_code, headers)

        return Response(
            resp.iter_content(chunk_size=1024),
            status=resp.status_code,
            headers=headers
        )

    except Exception as e:
        return f"Proxy Error: {str(e)}"


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

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id,username,role FROM users")
    users = cursor.fetchall()
    conn.close()

    result = run_cmd(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Ports}}|{{.Status}}"])

    containers = []

    for line in result.stdout.strip().split("\n"):
        if line:
            parts = line.split("|")
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
@app.route("/dashboard")
def dashboard():

    if not login_required():
        return redirect("/login")

    repo_filter = request.args.get("repo")
    user_filter = request.args.get("user")
    risk_filter = request.args.get("risk")

    history = load_history()

    if current_role() == "user":
        history = [h for h in history if h.get("user") == current_user()]

    if repo_filter:
        history = [h for h in history if repo_filter.lower() in h.get("repo", "").lower()]

    if user_filter:
        history = [h for h in history if user_filter.lower() in h.get("user", "").lower()]

    if risk_filter:
        history = [h for h in history if h.get("risk") == risk_filter]

    total = len(history)

    low    = sum(1 for h in history if h.get("risk") == "LOW")
    medium = sum(1 for h in history if h.get("risk") == "MEDIUM")
    high   = sum(1 for h in history if h.get("risk") == "HIGH")

    success  = sum(1 for h in history if "LIVE"     in h.get("status", ""))
    rollback = sum(1 for h in history if "ROLLBACK" in h.get("status", ""))
    failed   = sum(1 for h in history if "FAILED"   in h.get("status", ""))

    times = [h.get("time", 0) for h in history if isinstance(h.get("time"), (int, float))]
    avg_time     = round(sum(times) / len(times), 2) if times else 0
    failure_rate = round((failed / total) * 100, 2)  if total else 0

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

    user_stats   = {}
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
        user_stats=user_stats,
        role=current_role(),
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
    repo_url   = request.form["repo_url"]

    if not is_docker_running():
        return render_template(
            "result.html",
            level="N/A",
            status="DOCKER NOT RUNNING",
            time=0,
            error="Please start Docker Desktop"
        )

    open("logs.txt", "w").close()
    write_log("🚀 Starting Deployment...")

    try:
        safe_delete_clone()

        write_log("📥 Cloning repository...")
        clone_dir = get_clone_dir()

        Repo.clone_from(repo_url, clone_dir)
        repo = Repo(clone_dir)

        commits = list(repo.iter_commits())
        changed = []
        lines   = 0

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

        write_log("🔍 Analyzing code changes...")
        risk  = calculate_risk(changed, lines, repo_url)
        level = "LOW" if risk <= 40 else "MEDIUM" if risk <= 80 else "HIGH"
        write_log(f"⚠️ Risk Level: {level} (score: {risk})")

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

        write_log("🐳 Building Docker image...")
        success, error = docker_build()

        if not success:
            write_log("❌ Build Failed")
            status = "BUILD FAILED"
        else:
            write_log("✅ Build Successful")

            if level == "LOW":
                write_log("🟢 LOW risk — Normal deployment...")
                backup_current_container()
                status = deploy_main(port)

            elif level == "MEDIUM":
                write_log("🟡 MEDIUM risk — Canary deployment...")
                backup_current_container()
                status = deploy_canary(port)

            else:  # HIGH
                write_log("🔴 HIGH risk — Rolling back to last stable version...")
                status = deploy_backup(port)
                if "BLOCKED" in status:
                    write_log("⚠️ No backup found — this was the first deploy")
                    status = "HIGH RISK — No backup available. Please review your changes."
                else:
                    write_log("↩️ Rolled back successfully")

        total_time = round(time.time() - start_time, 2)

        save_history({
            "user": current_user(),
            "repo": repo_url,
            "risk": level,
            "status": status,
            "time": total_time
        })

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


# ---------------- WEBHOOK ----------------
@app.route("/webhook", methods=["POST"])
def github_webhook():

    if not is_docker_running():
        write_log("❌ Docker not running (Webhook)")
        return "Docker not running", 500

    data = request.json

    if data.get("ref") != "refs/heads/main":
        return "Ignored (not main branch)", 200

    repo_url       = data["repository"]["clone_url"]
    repo_url_clean = repo_url.replace(".git", "").lower().strip()

    write_log(f"🔔 Webhook received for: {repo_url}")

    history    = load_history()
    repo_owner = None
    old_port   = None

    for h in reversed(history):
        history_repo_clean = h.get("repo", "").replace(".git", "").lower().strip()
        repo_match = (
            repo_url_clean == history_repo_clean or
            repo_url_clean in history_repo_clean or
            history_repo_clean in repo_url_clean
        )

        if repo_match:
            if not repo_owner:
                repo_owner = h.get("user")

            if old_port is None and \
               ("LIVE" in h.get("status", "") or "ROLLBACK" in h.get("status", "")) and \
               "/app/" in h.get("status", ""):
                try:
                    port_match = re.search(r'/app/(\d+)', h["status"])
                    if port_match:
                        old_port = int(port_match.group(1))
                except:
                    pass

            if repo_owner and old_port:
                break

    if not repo_owner:
        write_log(f"❌ Webhook blocked — repo not found in history")
        write_log(f"❌ GitHub sent: {repo_url}")
        write_log(f"❌ History repos: {[h.get('repo') for h in history]}")
        return "Repo not registered", 403

    write_log(f"👤 Owner: {repo_owner}")
    write_log(f"🔁 Reusing port: {old_port if old_port else 'new port'}")

    try:
        clone_dir  = f"cloned_repo_{repo_owner}"
        image_name = f"{repo_owner}_image"

        # clean old clone
        if os.path.exists(clone_dir):
            try:
                shutil.rmtree(clone_dir, onerror=remove_readonly)
            except:
                time.sleep(2)
                try:
                    shutil.rmtree(clone_dir, onerror=remove_readonly)
                except Exception as e:
                    write_log(f"⚠️ Could not delete old clone: {str(e)}")

        write_log("📥 Cloning (Webhook)...")
        Repo.clone_from(repo_url, clone_dir)

        # detect project type
        project_type = "Unsupported"
        port         = None

        for root, dirs, files in os.walk(clone_dir):
            if "requirements.txt" in files:
                project_type, port = "Python", "5000"
                break
            if "package.json" in files:
                project_type, port = "Node.js", "3000"
                break
            if any(f.endswith(".html") for f in files):
                project_type, port = "Static Website", "80"
                break
            if any(f.endswith(".java") for f in files) or any(f.endswith(".jar") for f in files):
                project_type, port = "Java", "8080"
                break

        if project_type == "Unsupported":
            write_log("❌ Unsupported project type")
            return "Unsupported", 200

        # generate dockerfile if missing
        dockerfile_path = os.path.join(clone_dir, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            session["username"] = repo_owner
            generate_dockerfile(project_type)
            session.pop("username", None)

        write_log("🐳 Building Docker image (Webhook)...")
        run_cmd(["docker", "rmi", "-f", image_name])
        build_result = run_cmd(["docker", "build", "-t", image_name, "."], cwd=clone_dir)

        if build_result.returncode != 0:
            write_log("❌ Build failed (Webhook)")
            write_log(build_result.stderr)
            return "Build Failed", 500

        write_log("✅ Build successful")

        # stop all old containers for this user
        write_log(f"🛑 Stopping all containers for: {repo_owner}")
        all_containers = run_cmd(
            ["docker", "ps", "-a", "--format", "{{.Names}}"]
        ).stdout.strip().split("\n")

        for c in all_containers:
            if c and c.startswith(f"{repo_owner}_"):
                write_log(f"🛑 Removing: {c}")
                run_cmd(["docker", "rm", "-f", c])

        # deploy on same port as before
        deploy_port    = old_port if old_port else get_next_port()
        internal_port  = port if port != "0" else None
        container_name = f"{repo_owner}_{MAIN_CONTAINER}_{deploy_port}"

        write_log(f"🚀 Deploying on port {deploy_port}...")
        deploy_result = run_cmd([
            "docker", "run", "-d",
            "--name", container_name,
            *(["-p", f"{deploy_port}:{internal_port}"] if internal_port else []),
            image_name
        ])

        write_log(deploy_result.stdout)
        write_log(deploy_result.stderr)

        base_url = get_base_url()
        status   = f"LIVE → {base_url}/app/{deploy_port}"

        # save to history so next webhook finds same port
        all_history = load_history()
        all_history.append({
            "user": repo_owner,
            "repo": repo_url,
            "risk": "AUTO",
            "status": status,
            "time": 0
        })
        with open(HISTORY_FILE, "w") as f:
            json.dump(all_history, f, indent=4)

        write_log(f"✅ Webhook deployment done → {status}")
        return "Success", 200

    except Exception as e:
        write_log(f"❌ Webhook error: {str(e)}")
        return "Error", 500


# ---------------- SYSTEM CONTROL ----------------
@app.route("/system-control")
def system_control():

    if current_role() not in ["admin", "superadmin"]:
        return "Access Denied"

    result = run_cmd(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Ports}}|{{.Status}}"])

    containers = []

    for line in result.stdout.strip().split("\n"):
        if line:
            parts = line.split("|")
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
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)