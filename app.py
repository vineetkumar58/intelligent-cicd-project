import os
import shutil
import stat
import subprocess
import json
import time
from flask import Flask, render_template, request
from git import Repo

app = Flask(__name__)

CLONE_DIR = "cloned_repo"
IMAGE_NAME = "intelligent_app_image"
MAIN_CONTAINER = "intelligent_app_main"
BACKUP_IMAGE = "intelligent_backup_image"
STATE_FILE = "last_state.json"
HISTORY_FILE = "deployment_history.json"


# ---------------- UTILITIES ----------------
def remove_readonly(func, path, _):
    os.chmod(path, stat.S_IWRITE)
    func(path)

def run_cmd(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def safe_delete_clone():
    if os.path.exists(CLONE_DIR):
        shutil.rmtree(CLONE_DIR, onerror=remove_readonly)

def stop_container(name):
    run_cmd(["docker", "rm", "-f", name])


# ---------------- HISTORY ----------------
def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r") as f:
        return json.load(f)

def save_history(entry):
    history = load_history()
    history.append(entry)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=4)


# ---------------- STATE MEMORY ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=4)


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

    if project_type == "Python":
        content = """FROM python:3.10
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt || true
EXPOSE 5000
CMD ["python","app.py"]
"""
    elif project_type == "Node.js":
        content = """FROM node:18
WORKDIR /app
COPY . .
RUN npm install || true
EXPOSE 3000
CMD ["npm","start"]
"""
    elif project_type == "Static Website":
        content = """FROM nginx:latest
COPY . /usr/share/nginx/html
EXPOSE 80
"""
    else:
        return False

    with open(dockerfile, "w") as f:
        f.write(content)

    return True


# ---------------- DOCKER ----------------
def docker_build():
    run_cmd(["docker","rmi","-f",IMAGE_NAME])
    result = run_cmd(["docker","build","-t",IMAGE_NAME,"."], cwd=CLONE_DIR)
    return result.returncode == 0

def backup_current_container():
    running = run_cmd(["docker","ps","-q","-f",f"name={MAIN_CONTAINER}"]).stdout.strip()
    if running:
        run_cmd(["docker","commit",MAIN_CONTAINER,BACKUP_IMAGE])

def deploy_main(port):
    stop_container(MAIN_CONTAINER)
    run_cmd(["docker","run","-d","--name",MAIN_CONTAINER,"-p",port,IMAGE_NAME])
    return f"LIVE → http://127.0.0.1:5001"

def deploy_backup(port):
    exists = run_cmd(["docker","images","-q",BACKUP_IMAGE]).stdout.strip()
    if not exists:
        return "BLOCKED (No stable version yet)"

    stop_container(MAIN_CONTAINER)
    run_cmd(["docker","run","-d","--name",MAIN_CONTAINER,"-p",port,BACKUP_IMAGE])
    return f"ROLLBACK → http://127.0.0.1:5001"


# ---------------- RISK ENGINE ----------------
def calculate_risk(files, lines):
    score=0
    for f in files:
        f=f.lower()
        if f.endswith(('.py','.js','.java')): score+=20
        if "auth" in f: score+=40
        if "config" in f: score+=30
        if "db" in f or f.endswith(".sql"): score+=40

    if lines>200: score+=40
    elif lines>50: score+=20

    return score


# ---------------- ROUTES ----------------
@app.route('/')
def home():
    return render_template("index.html")


@app.route('/dashboard')
def dashboard():
    history = load_history()
    total=len(history)
    low=sum(1 for h in history if h["risk"]=="LOW")
    medium=sum(1 for h in history if h["risk"]=="MEDIUM")
    high=sum(1 for h in history if h["risk"]=="HIGH")

    return render_template("dashboard.html",
        total=total, low=low, medium=medium, high=high)


@app.route('/analyze', methods=['POST'])
def analyze():

    start_time = time.time()
    repo_url = request.form['repo_url']

    try:
        safe_delete_clone()
        Repo.clone_from(repo_url,CLONE_DIR)
        repo=Repo(CLONE_DIR)

        latest_commit=str(repo.head.commit.hexsha)
        state=load_state()

        if state.get("last_commit")==latest_commit:
            total_time = round(time.time() - start_time,2)
            save_history({"repo":repo_url,"risk":"SKIPPED","status":"Skipped","time":total_time})
            return f"<h2>No New Changes Detected</h2>Execution Time: {total_time} sec"

        project_type, port = detect_project_type()

        if project_type=="Unsupported":
            return "<h2>Repository analyzed but not deployable</h2>"

        generate_dockerfile(project_type)

        commits=list(repo.iter_commits())
        changed=[]
        lines=0

        if len(commits)>=2:
            latest,prev=commits[0],commits[1]
            diff=latest.diff(prev)
            for c in diff:
                if c.a_path: changed.append(c.a_path)

            raw=repo.git.diff(prev,latest)
            for l in raw.split("\n"):
                if (l.startswith("+") and not l.startswith("+++")) or (l.startswith("-") and not l.startswith("---")):
                    lines+=1

        risk=calculate_risk(changed,lines)
        level="LOW" if risk<=20 else "MEDIUM" if risk<=60 else "HIGH"

        if not docker_build():
            save_history({"repo":repo_url,"risk":level,"status":"BUILD FAILED","time":0})
            return "<h2>Build failed</h2>"

        if level in ["LOW","MEDIUM"]:
            backup_current_container()
            status=deploy_main(port)
            state["last_commit"]=latest_commit
        else:
            status=deploy_backup(port)

        save_state(state)
        total_time = round(time.time() - start_time,2)

        save_history({"repo":repo_url,"risk":level,"status":status,"time":total_time})

        return f"""
        <h2>Intelligent CI/CD Result</h2>
        Risk Level: {level}<br>
        Status: {status}<br>
        Execution Time: {total_time} sec
        """

    except Exception as e:
        return f"Error: {str(e)}"


if __name__=="__main__":
    app.run(debug=True, use_reloader=False)
