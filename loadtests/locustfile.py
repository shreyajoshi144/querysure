from __future__ import annotations

import io
import json
import uuid

from locust import HttpUser, between, task


class QuerySureUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.email = f"load-{uuid.uuid4().hex[:12]}@example.com"
        self.password = "LoadTestPassword123!"
        r = self.client.post("/api/auth/register", json={"email": self.email, "password": self.password}, name="auth/register")
        if r.status_code not in (200, 409):
            return
        # Test environments can use EMAIL_PROVIDER=dev; verification itself is
        # intentionally omitted here because it depends on the test mailbox.
        r = self.client.post("/api/auth/login", json={"email": self.email, "password": self.password}, name="auth/login")
        if r.status_code == 200:
            self.token = r.json().get("access_token")
        else:
            self.token = None
        self.session_id = None

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _ensure_session(self):
        if self.session_id or not self.token:
            return False
        r = self.client.post("/api/session", headers=self.headers, name="session/create")
        if r.status_code == 200:
            self.session_id = r.json().get("session_id")
            return True
        return False

    @task(2)
    def sample_connect(self):
        if not self._ensure_session():
            return
        self.client.post("/api/connect/sample", json={"session_id": self.session_id}, headers=self.headers, name="connect/sample")

    @task(1)
    def query_pipeline(self):
        if not self._ensure_session():
            return
        q = {"session_id": self.session_id, "question": "What are the top five products by sales?"}
        self.client.post("/api/schema-intelligence", json=q, headers=self.headers, name="pipeline/schema")
        self.client.post("/api/semantic-layer", json=q, headers=self.headers, name="pipeline/semantic")
        self.client.post("/api/retrieve-tables", json=q, headers=self.headers, name="pipeline/retrieve")

    @task(1)
    def upload(self):
        if not self._ensure_session():
            return
        body = b"id,name,amount\n1,Widget,10\n2,Gadget,20\n"
        files = {"file": ("load.csv", io.BytesIO(body), "text/csv")}
        with self.client.post("/api/connect/upload", files=files, data={"session_id": self.session_id}, headers=self.headers, name="connect/upload", catch_response=True) as r:
            if r.status_code == 202:
                job_id = r.json().get("job_id")
                if job_id:
                    self.client.get(f"/api/jobs/{job_id}", headers=self.headers, name="jobs/status")
