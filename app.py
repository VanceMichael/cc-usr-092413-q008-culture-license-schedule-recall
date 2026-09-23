from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/api/v1/schedules")
def schedules():
    return jsonify(items=[])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
