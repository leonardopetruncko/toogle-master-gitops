# Runbook: auth-service com taxa de 5xx acima de 5%

| Campo | Valor |
|---|---|
| Alerta | `auth-service - taxa de erros 5xx acima de 5%` (uid `auth-5xx-rate`) |
| Severidade | `critical`: abre incidente no PagerDuty e notifica o Discord |
| Fonte | Métricas RED do connector `spanmetrics` (OTel Collector) no Prometheus |
| Condição | 5xx / total de requests do auth-service **> 5% por 1 minuto** |
| Automação | **Self-healer**: `rollout restart deployment/auth-service` (cooldown de 5 min) |

## Por que este alerta importa

O auth-service valida as API keys de **todos** os outros serviços: `flag-service` e
`targeting-service` chamam `/validate` a cada request, e o `evaluation-service` depende
dos dois. Com o auth falhando, a avaliação de feature flags para no sistema inteiro.

A falha costuma ser **silenciosa**: o `/health` continua 200, então liveness e readiness
não reiniciam o pod. Só a taxa de erro das requisições reais mostra o problema.

## O que acontece automaticamente (sem humano)

1. O Grafana avalia a regra a cada 30s. Acima de 5% por 1 min, o alerta vai para **FIRING**.
2. A notification policy envia para 3 destinos (agrupados por `alertname` + `service`):
   - **PagerDuty**: abre o incidente (Events API v2) e acorda quem está de plantão;
   - **Discord**: mensagem com serviço, taxa atual, ação automática e este runbook;
   - **Self-healer** (webhook interno): executa o runbook automatizado.
3. O self-healer confere a allowlist e o cooldown e faz o `rollout restart` (patch na
   annotation `kubectl.kubernetes.io/restartedAt`, igual ao `kubectl rollout restart`).
4. Ele acompanha o rollout e posta no Discord "Self-healing concluído" (ou "sem sucesso").
5. Com os pods novos a taxa volta ao normal, o alerta vai para **Resolved** e o PagerDuty
   resolve o incidente sozinho.

## O que o humano faz (se a automação não resolver)

O restart resolve estado corrompido em memória, vazamento e conexões presas. Ele **não**
resolve banco fora do ar, credencial inválida ou bug em versão nova. Se o alerta voltar
a disparar depois do cooldown, investigue:

```powershell
# 1. O que o self-healer fez
kubectl logs deploy/self-healer -n observability --tail=50

# 2. Estado dos pods e eventos
kubectl get pods -n toogle-master -l app=auth-service
kubectl describe deploy/auth-service -n toogle-master

# 3. Logs de erro do auth (também no Grafana > Explore > Loki)
kubectl logs deploy/auth-service -n toogle-master --tail=100 | Select-String -Pattern "ERRO|CHAOS"
```

- **Erro de banco** (`ERRO ao consultar o banco`): verifique o RDS `auth-db` (status e
  Security Group `data-sg`).
- **Começou depois de um deploy**: faça rollback pelo Git (reverta o commit da tag no
  repositório GitOps e o ArgoCD sincroniza).
- **Trace do problema**: no Datadog > APM > auth-service > Traces, filtre por erro. O `trace_id` também aparece nas linhas de log do Loki.

## Pós-incidente

Registre no incidente do PagerDuty: início, detecção (horário do FIRING), ação automática,
resolução (MTTR) e causa raiz. Se o restart não resolveu, abra uma tarefa para uma
correção definitiva e avalie se o alerta ou o runbook precisam de ajuste.
