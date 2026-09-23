"""Self-healer do ToggleMaster (Runbook Automation disparado por webhook).

Fluxo: Grafana Alerting -> POST /webhook -> rollout restart do deployment afetado.

O restart é o mesmo que o `kubectl rollout restart deployment/<nome>` faz: um patch na
annotation kubectl.kubernetes.io/restartedAt do template do pod, feito direto na API do
Kubernetes com o token da ServiceAccount (RBAC restrito a get/patch de deployments no
namespace toogle-master). Só usa a biblioteca padrão do Python, então roda na imagem
oficial python:*-slim, sem build nem registry.

Proteções contra "remediação em loop" (a versão automática do alert fatigue):
  - ALLOWED_DEPLOYMENTS: só reinicia o que está na allowlist;
  - COOLDOWN_SECONDS: no máximo um restart por deployment dentro da janela;
  - só age em alertas com status "firing" (o "resolved" é só registrado).
"""

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [self-healer] %(message)s")
log = logging.getLogger("self-healer")

PORT = int(os.getenv("PORT", "8080"))
ALLOWED_NAMESPACES = {n.strip() for n in os.getenv("ALLOWED_NAMESPACES", "toogle-master").split(",") if n.strip()}
ALLOWED_DEPLOYMENTS = {d.strip() for d in os.getenv("ALLOWED_DEPLOYMENTS", "").split(",") if d.strip()}
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "300"))
ROLLOUT_TIMEOUT_SECONDS = int(os.getenv("ROLLOUT_TIMEOUT_SECONDS", "180"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

SA_DIR = os.getenv("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
K8S_API = os.getenv(
    "K8S_API",
    f"https://{os.getenv('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')}:{os.getenv('KUBERNETES_SERVICE_PORT', '443')}",
)

_last_restart = {}
_lock = threading.Lock()


def _k8s_request(method, path, body=None, content_type="application/json"):
    """Chamada autenticada na API do Kubernetes usando a ServiceAccount do pod."""
    with open(f"{SA_DIR}/token", encoding="utf-8") as f:
        token = f.read().strip()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(K8S_API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", content_type)
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt") if K8S_API.startswith("https") else None
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def notify_discord(message):
    """Registra no canal do ChatOps o que a automação fez (se houver webhook configurado)."""
    if not DISCORD_WEBHOOK_URL:
        return
    body = json.dumps({"username": "self-healer", "content": message}).encode()
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    # O Discord recusa o User-Agent padrão do urllib.
    req.add_header("User-Agent", "toggle-master-self-healer/1.0")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Falha ao notificar o Discord: %s", e)


def rollout_restart(namespace, deployment):
    """Equivalente a `kubectl rollout restart deployment/<deployment> -n <namespace>`."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
    _k8s_request(
        "PATCH",
        f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}",
        patch,
        content_type="application/strategic-merge-patch+json",
    )
    return now


def wait_rollout(namespace, deployment, started):
    """Acompanha o rollout (como o `kubectl rollout status`) e registra o resultado."""
    deadline = time.time() + ROLLOUT_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(5)
        try:
            dep = _k8s_request("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}")
        except (urllib.error.URLError, TimeoutError) as e:
            log.warning("Erro ao consultar o rollout de %s: %s", deployment, e)
            continue
        spec_replicas = dep.get("spec", {}).get("replicas", 1)
        status = dep.get("status", {})
        done = (
            status.get("observedGeneration", 0) >= dep.get("metadata", {}).get("generation", 0)
            and status.get("updatedReplicas", 0) == spec_replicas
            and status.get("availableReplicas", 0) == spec_replicas
            and status.get("replicas", 0) == spec_replicas
        )
        if done:
            seconds = int(time.time() - started)
            log.info("ROLLOUT CONCLUIDO deployment/%s em %ss (%s/%s pods novos disponíveis)", deployment, seconds, spec_replicas, spec_replicas)
            notify_discord(f"✅ **Self-healing concluído**: `deployment/{deployment}` reiniciado em {seconds}s, {spec_replicas}/{spec_replicas} pods novos disponíveis. Acompanhe a taxa de erros no Grafana.")
            return
    log.error("ROLLOUT NAO CONCLUIU deployment/%s em %ss: escalar para humano", deployment, ROLLOUT_TIMEOUT_SECONDS)
    notify_discord(f"⚠️ **Self-healing sem sucesso**: o rollout de `deployment/{deployment}` não concluiu em {ROLLOUT_TIMEOUT_SECONDS}s. O incidente segue aberto no PagerDuty para ação humana.")


def handle_alert(alert):
    """Decide e executa a ação para um alerta. Retorna um dict com o que foi feito."""
    labels = alert.get("labels", {})
    name = labels.get("alertname", "?")
    namespace = labels.get("namespace", "")
    deployment = labels.get("deployment", "")
    status = alert.get("status", "")

    if status != "firing":
        log.info("Alerta '%s' com status '%s': nada a fazer.", name, status)
        return {"alert": name, "action": "none", "reason": f"status {status}"}
    if namespace not in ALLOWED_NAMESPACES or deployment not in ALLOWED_DEPLOYMENTS:
        log.warning("Alerta '%s' ignorado: %s/%s fora da allowlist.", name, namespace, deployment)
        return {"alert": name, "action": "none", "reason": "fora da allowlist"}

    key = f"{namespace}/{deployment}"
    with _lock:
        elapsed = time.time() - _last_restart.get(key, 0)
        if elapsed < COOLDOWN_SECONDS:
            wait = int(COOLDOWN_SECONDS - elapsed)
            log.info("Cooldown ativo para %s (faltam %ss): restart NAO executado.", key, wait)
            return {"alert": name, "action": "skipped", "reason": f"cooldown ({wait}s restantes)"}
        _last_restart[key] = time.time()

    log.info("RUNBOOK: alerta '%s' disparou -> kubectl rollout restart deployment/%s -n %s", name, deployment, namespace)
    started = time.time()
    try:
        restarted_at = rollout_restart(namespace, deployment)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        with _lock:
            _last_restart.pop(key, None)
        log.error("Falha ao reiniciar %s: %s", key, e)
        notify_discord(f"❌ **Self-healing falhou** ao reiniciar `deployment/{deployment}`: {e}")
        return {"alert": name, "action": "error", "reason": str(e)}

    log.info("Rollout restart aplicado em deployment/%s (restartedAt=%s). Acompanhando o rollout...", deployment, restarted_at)
    notify_discord(
        f"🔧 **Self-healing acionado** pelo alerta *{name}*\n"
        f"Ação: `kubectl rollout restart deployment/{deployment} -n {namespace}`\n"
        f"Motivo: {alert.get('annotations', {}).get('summary', '')}"
    )
    threading.Thread(target=wait_rollout, args=(namespace, deployment, started), daemon=True).start()
    return {"alert": name, "action": "rollout_restart", "deployment": key, "restartedAt": restarted_at}


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/webhook":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "payload inválido"})
            return
        alerts = payload.get("alerts", [])
        log.info("Webhook recebido do Grafana: status=%s, %d alerta(s).", payload.get("status"), len(alerts))
        results = [handle_alert(a) for a in alerts]
        self._reply(200, {"results": results})

    def log_message(self, fmt, *args):
        # Silencia o access log padrão (o /healthz das probes poluiria o Loki).
        pass


def main():
    log.info(
        "Self-healer ouvindo na porta %s | namespaces=%s | deployments=%s | cooldown=%ss",
        PORT,
        sorted(ALLOWED_NAMESPACES),
        sorted(ALLOWED_DEPLOYMENTS),
        COOLDOWN_SECONDS,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
