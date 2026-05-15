"""
tests/locustfile.py
Locust load test for the Pollux / VoiceWave API.

Run (web UI):
    locust -f tests/locustfile.py --host http://localhost:8004

Run (headless, 20 users, 2 min):
    locust -f tests/locustfile.py --host http://localhost:8004 \
           --headless -u 20 -r 2 --run-time 2m \
           --html tests/report.html

Install:
    pip install locust
"""
import random
import string
import time

from locust import HttpUser, between, task, events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _random_email():
    tag = "".join(random.choices(string.ascii_lowercase, k=8))
    return f"loadtest_{tag}@example.com"


def _random_lyrics():
    lines = [
        "Walking through the city lights alone",
        "Every step I take I feel the beat",
        "Lost in the rhythm of the night",
        "Dancing with the shadows in my mind",
        "Chasing dreams until the morning comes",
    ]
    return "\n".join(random.sample(lines, k=3))


# ---------------------------------------------------------------------------
# User behaviour classes
# ---------------------------------------------------------------------------


class AuthOnlyUser(HttpUser):
    """Simulates users that only hit auth and read-only endpoints.
    These should be very fast and form the majority of traffic.
    """
    wait_time = between(0.5, 2)
    weight = 6  # 60 % of virtual users

    def on_start(self):
        self._register_and_login()

    def _register_and_login(self):
        email = _random_email()
        password = "LoadTest@123"
        # Register
        r = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": password},
            name="/api/auth/register",
        )
        if r.status_code == 200:
            self.token = r.json().get("access_token", "")
        else:
            # Fall back: try a fixed test account
            r2 = self.client.post(
                "/api/auth/login",
                json={"email": "test@example.com", "password": "Test@12345"},
                name="/api/auth/login [fallback]",
            )
            self.token = r2.json().get("access_token", "") if r2.status_code == 200 else ""

        self.headers = {"Authorization": f"Bearer {self.token}"}

    @task(3)
    def get_me(self):
        self.client.get("/api/auth/me", headers=self.headers, name="/api/auth/me")

    @task(2)
    def get_history(self):
        self.client.get(
            "/api/history?page=1&per_page=10",
            headers=self.headers,
            name="/api/history",
        )

    @task(1)
    def get_stats(self):
        self.client.get("/api/stats", headers=self.headers, name="/api/stats")

    @task(1)
    def get_music_options(self):
        self.client.get(
            "/api/music/options",
            headers=self.headers,
            name="/api/music/options",
        )

    @task(1)
    def login_logout_cycle(self):
        email = _random_email()
        self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "LoadTest@123"},
            name="/api/auth/register",
        )
        self.client.post(
            "/api/auth/login",
            json={"email": email, "password": "LoadTest@123"},
            name="/api/auth/login",
        )


class SongSubmitUser(HttpUser):
    """Simulates users that submit a song job and poll until done (or timeout).
    These are expensive — keep weight low.
    """
    wait_time = between(5, 15)
    weight = 2  # 20 % of virtual users

    def on_start(self):
        email = _random_email()
        r = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "LoadTest@123"},
            name="/api/auth/register",
        )
        self.token = r.json().get("access_token", "") if r.status_code == 200 else ""
        self.headers = {"Authorization": f"Bearer {self.token}"}

    @task
    def submit_and_poll_song(self):
        if not self.token:
            return

        # Submit job
        r = self.client.post(
            "/api/generate_music",
            json={
                "lyrics": _random_lyrics(),
                "style": random.choice(["pop", "lofi", "acoustic"]),
                "audio_duration": 15,   # shortest possible — we just want timing data
                "quality": "turbo",
            },
            headers=self.headers,
            name="/api/generate_music [submit]",
        )
        if r.status_code != 200:
            return

        job_id = r.json().get("job_id")
        if not job_id:
            return

        # Poll for up to 90 s (turbo mode) without blocking Locust's event loop
        poll_url = f"/api/jobs/{job_id}"
        deadline = time.time() + 90
        while time.time() < deadline:
            p = self.client.get(
                poll_url,
                headers=self.headers,
                name="/api/jobs/{id} [poll]",
            )
            if p.status_code != 200:
                break
            status = p.json().get("status", "")
            if status in ("done", "failed"):
                break
            time.sleep(3)


class HistoryHeavyUser(HttpUser):
    """Simulates read-heavy users paging through history and downloading audio.
    """
    wait_time = between(1, 3)
    weight = 2  # 20 % of virtual users

    def on_start(self):
        email = _random_email()
        r = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "LoadTest@123"},
            name="/api/auth/register",
        )
        self.token = r.json().get("access_token", "") if r.status_code == 200 else ""
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.generation_ids: list[int] = []

    @task(4)
    def page_history(self):
        page = random.randint(1, 3)
        r = self.client.get(
            f"/api/history?page={page}&per_page=20",
            headers=self.headers,
            name="/api/history",
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            self.generation_ids = [i["id"] for i in items if "id" in i]

    @task(1)
    def download_audio(self):
        if not self.generation_ids:
            return
        gen_id = random.choice(self.generation_ids)
        self.client.get(
            f"/api/audio/{gen_id}",
            headers=self.headers,
            name="/api/audio/{id}",
        )

    @task(1)
    def get_job_status(self):
        # Just checks a plausible endpoint; may 404 if no job — that's fine.
        self.client.get(
            "/api/jobs/nonexistent",
            headers=self.headers,
            name="/api/jobs/{id} [miss]",
        )


# ---------------------------------------------------------------------------
# Custom event: print a summary line on test stop
# ---------------------------------------------------------------------------

@events.quitting.add_listener
def on_quitting(environment, **_kw):
    stats = environment.stats
    total = stats.total
    print(
        f"\n{'='*60}\n"
        f"Requests : {total.num_requests}\n"
        f"Failures : {total.num_failures}  ({total.fail_ratio*100:.1f} %)\n"
        f"Median   : {total.median_response_time} ms\n"
        f"95th pct : {total.get_response_time_percentile(0.95)} ms\n"
        f"99th pct : {total.get_response_time_percentile(0.99)} ms\n"
        f"RPS      : {total.current_rps:.1f}\n"
        f"{'='*60}"
    )
