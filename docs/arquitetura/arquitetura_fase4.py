# Diagrama de arquitetura do ToggleMaster - Fase 4 (Observabilidade e Self-Healing).
# Gerado com a lib "diagrams" (diagram as code). Uso:
#   python arquitetura_fase4.py [caminho_de_saida_sem_extensao]
import os
import sys

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import ECR
from diagrams.aws.database import Dynamodb, ElasticacheForRedis, RDSPostgresqlInstance
from diagrams.aws.integration import SQS
from diagrams.custom import Custom
from diagrams.k8s.compute import Pod
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.gitops import Argocd
from diagrams.onprem.logging import Loki
from diagrams.onprem.monitoring import Datadog, Grafana, Prometheus
from diagrams.onprem.vcs import Github
from diagrams.programming.language import Go, Python
from diagrams.saas.alerting import Pagerduty
from diagrams.saas.chat import Discord

AQUI = os.path.dirname(os.path.abspath(__file__))
OTEL_ICON = os.path.join(AQUI, "otel.png")
SAIDA = sys.argv[1] if len(sys.argv) > 1 else os.path.join(AQUI, "Projeto-Fase4")

graph_attr = {
    "fontsize": "26",
    "fontname": "Helvetica-Bold",
    "labelloc": "t",
    "pad": "0.8",
    "nodesep": "0.7",
    "ranksep": "1.3",
    "splines": "spline",
    "bgcolor": "white",
    "newrank": "true",
}
node_attr = {"fontname": "Helvetica", "fontsize": "13"}
edge_attr = {"fontname": "Helvetica", "fontsize": "11", "penwidth": "1.6"}

# Cada tipo de sinal tem a sua cor.
TRACE = "#7B42BC"    # telemetria OTLP / traces
METRICA = "#E6522C"  # métricas
LOG = "#D4A000"      # logs
ALERTA = "#D0021B"   # alerta / incidente / self-healing
DEPLOY = "#1F6FEB"   # CI/CD e GitOps
APP = "#6B7280"      # chamadas entre serviços


def cor(c, **kw):
    return Edge(color=c, fontcolor=c, **kw)


with Diagram(
    "ToggleMaster - Fase 4: Observabilidade Total e Resposta Ativa",
    filename=SAIDA,
    show=False,
    direction="LR",
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
    outformat="png",
):
    with Cluster("Entrega contínua (Fases 2 e 3)"):
        ci = GithubActions("GitHub Actions\nCI + DevSecOps")
        gitops = Github("Repo GitOps\nmanifests + Helm charts")
        ecr = ECR("ECR\n5 repositórios")

    with Cluster("AWS us-east-1  |  VPC + EKS provisionados via Terraform"):
        with Cluster("EKS toogle-cluster  (2x t3.large, subnets privadas)"):
            argocd = Argocd("ArgoCD\nApp of Apps")

            with Cluster("ns toogle-master  |  apps instrumentadas com OpenTelemetry SDK"):
                evaluation = Go("evaluation-service")
                flag = Python("flag-service")
                targeting = Python("targeting-service")
                auth = Go("auth-service")
                analytics = Python("analytics-service")

            with Cluster("ns observability  |  Helm charts sincronizados pelo ArgoCD"):
                otel = Custom("OTel Collector\n(DaemonSet)\notlp + filelog", OTEL_ICON)
                prom = Prometheus("Prometheus\nmétricas + spanmetrics")
                loki = Loki("Loki\nlogs centralizados")
                grafana = Grafana("Grafana\ndashboard + alertas")
                healer = Pod("self-healer\nRunbook Automation")

        with Cluster("Dados gerenciados"):
            redis = ElasticacheForRedis("ElastiCache Redis")
            sqs = SQS("SQS toogle-events")
            rds = RDSPostgresqlInstance("RDS PostgreSQL x3")
            dynamo = Dynamodb("DynamoDB")

    with Cluster("SaaS  |  APM e resposta a incidentes"):
        apm = Datadog("Datadog APM\nService Map + Traces")
        pagerduty = Pagerduty("PagerDuty\nincidente")
        discord = Discord("Discord\nChatOps")

    # 1. Entrega contínua (GitOps pull-based)
    ci >> cor(DEPLOY, label="build + scan + push") >> ecr
    ci >> cor(DEPLOY, label="atualiza tag") >> gitops
    gitops >> cor(DEPLOY, label="pull") >> argocd
    argocd >> cor(DEPLOY, style="dashed", label="sync") >> evaluation

    # 2. Caminho da requisição (trace distribuído)
    evaluation >> cor(APP) >> flag >> cor(APP) >> auth
    evaluation >> cor(APP) >> targeting >> cor(APP) >> auth
    evaluation >> cor(APP, style="dashed") >> redis
    evaluation >> cor(APP, label="evento + traceparent") >> sqs
    sqs >> cor(APP) >> analytics
    analytics >> cor(APP, style="dashed") >> dynamo
    [flag, targeting, auth] >> cor(APP, style="dashed") >> rds

    # 3. Telemetria: tudo passa pelo OTel Collector
    auth >> cor(TRACE, label="OTLP gRPC") >> otel
    [flag, targeting, analytics] >> cor(TRACE) >> otel
    otel >> cor(TRACE, label="traces + métricas + logs") >> apm
    otel >> cor(METRICA, label="remote write") >> prom
    otel >> cor(LOG, label="logs OTLP") >> loki
    prom >> cor(METRICA) >> grafana
    loki >> cor(LOG) >> grafana

    # 4. Resposta ativa: alerta -> incidente -> ChatOps -> self-healing
    grafana >> cor(ALERTA, label="5xx auth > 5% por 1 min") >> pagerduty
    grafana >> cor(ALERTA, label="notificação") >> discord
    grafana >> cor(ALERTA, label="webhook") >> healer
    healer >> cor(ALERTA, style="bold", label="kubectl rollout restart", constraint="false") >> auth
