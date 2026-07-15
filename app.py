import json
import subprocess

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

from agent import run_chat
from db import init_db, serialize
from dispatcher import dispatch
from issues import create_issue, delete_issue, get_issue, list_issues, update_issue

load_dotenv()

app = Flask(__name__)
CORS(app)


# ── Issues ───────────────────────────────────────────────────────────────────

@app.route("/issues", methods=["GET"])
def get_issues():
    return jsonify(list_issues())


@app.route("/issues", methods=["POST"])
def post_issue():
    data = request.get_json()
    if not data or not data.get("title"):
        return jsonify({"error": "title is required"}), 400
    issue = create_issue(
        title=data["title"],
        description=data.get("description", ""),
        status=data.get("status", "open"),
        repo_name=data.get("repo_name"),
        repo_url=data.get("repo_url"),
        repo_path=data.get("repo_path"),
    )
    return jsonify(issue), 201


@app.route("/issues/<int:issue_id>", methods=["PATCH"])
def patch_issue(issue_id):
    data = request.get_json() or {}
    issue = update_issue(issue_id, **data)
    if issue is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(issue)


@app.route("/issues/<int:issue_id>", methods=["DELETE"])
def remove_issue(issue_id):
    if not delete_issue(issue_id):
        return jsonify({"error": "not found"}), 404
    return '', 204


# ── GitHub issues ───────────────────────────────────────────────────────────

@app.route("/github/issues", methods=["GET"])
def get_github_issues():
    repo = request.args.get("repo")
    if not repo:
        return jsonify({"error": "repo query parameter is required (e.g. owner/repo)"}), 400

    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--json", "number,title,state,body,author,labels,assignees,createdAt,updatedAt",
        "--limit", request.args.get("limit", "30"),
    ]

    state = request.args.get("state")
    if state:
        cmd += ["--state", state]

    label = request.args.get("label")
    if label:
        cmd += ["--label", label]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return jsonify({"error": "gh CLI is not installed"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "gh command timed out"}), 504

    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip()}), 502

    return jsonify(json.loads(result.stdout))


# ── Dispatcher ───────────────────────────────────────────────────────────────

@app.route("/dispatch", methods=["POST"])
def dispatch_issue():
    data = request.get_json() or {}
    title = data.get("title", "")
    description = data.get("description", "")
    repo_path = data.get("repo_path", "")

    if not repo_path:
        return jsonify({"error": "repo_path is required"}), 400
    if not title:
        return jsonify({"error": "title is required"}), 400

    issue = {
        "title": title,
        "description": description,
        "repo_path": repo_path,
    }
    extra = data.get("instructions", "")

    return Response(
        stream_with_context(dispatch(issue, extra)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Agent sessions ───────────────────────────────────────────────────────────

@app.route("/agents", methods=["GET"])
def get_agents():
    from db import get_db
    conn = get_db()
    with conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM agent_sessions ORDER BY updated_at DESC")
            rows = cursor.fetchall()
    return jsonify([serialize(r) for r in rows])


# ── Agent chat ───────────────────────────────────────────────────────────────

@app.route("/agent/chat", methods=["POST"])
def agent_chat():
    data = request.get_json() or {}
    message  = data.get("message", "").strip()
    tasks    = data.get("tasks", [])
    repo     = data.get("repo", "")
    agent_id = data.get("agent_id")

    if not message:
        return jsonify({"error": "message is required"}), 400

    try:
        result = run_chat(message, tasks, agent_id, repo=repo)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=7777)
