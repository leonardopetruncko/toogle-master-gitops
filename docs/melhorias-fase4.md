# Melhorias e correções da Fase 4

Este documento registra tudo o que precisou ser ajustado no ToggleMaster para a Fase 4
funcionar de ponta a ponta, incluindo problemas herdados das fases anteriores que só
apareceram quando a observabilidade entrou. Cada item traz **problema → causa → solução**
e, quando houve, a **evidência** da validação feita no cluster real (sessão de 23/09/2026).

---

## 1. Entrega contínua: 4 dos 5 pipelines estavam quebrados

Antes da Fase 4, só o `auth-service` tinha imagem publicada no ECR. Os outros quatro
falhavam no CI, e por isso os manifests apontavam para a tag `:latest`, que nunca existiu.

| Serviço | Erro | Solução |
|---|---|---|
| flag, targeting, analytics | `ruff format --check` reprovava o `app.py` | Formatação aplicada (`ruff format`) |
| evaluation | `SA1019: io/ioutil has been deprecated` | Troca de `io/ioutil` por `io` |
| auth, evaluation | `the Go language version (go1.24) used to build golangci-lint is lower than the targeted Go version (1.26.0)` | `golangci-lint-action@v9` com golangci-lint **v2.13.2** (compilado com Go 1.27) e migração do `.golangci.yml` para o formato v2 |
| todos | `This run likely failed because of a workflow file issue` | A conta do GitHub foi renomeada (`drop0rz` → `leonardopetruncko`) e o Actions **não segue redirecionamento** em `uses:`. Referências atualizadas em 26 arquivos |

**Melhoria adicional:** os `ci.yml` ganharam `workflow_dispatch`. Como o `terraform destroy`
apaga o ECR ao fim de cada sessão do AWS Academy, as imagens precisam ser reconstruídas na
sessão seguinte sem depender de um commit novo.

**Evidência:** os 5 pipelines terminaram verdes, com push no ECR e atualização automática
das tags neste repositório (commits `chore(<serviço>): atualiza imagem para ...`).

---

## 2. Segurança

### 2.1 SSRF (CWE-918) no evaluation-service

O `gosec` apontou **G704 (HIGH)** em 4 pontos: o `flag_name` vinha do usuário e era
concatenado direto na URL das chamadas ao `flag-service` e ao `targeting-service`.

Correção em camadas:
- allowlist do nome da flag por regex (`^[a-zA-Z0-9._-]{1,100}$`) antes de montar a URL;
- `url.PathEscape` no segmento;
- resposta **400** no handler quando o nome é inválido (entrada ruim é erro do cliente e
  não deve poluir a taxa de 5xx que alimenta o alerta);
- `#nosec G704` documentado, já que o host sempre vem da configuração, nunca do usuário.

**Evidência:** `gosec -severity high -confidence high ./...` saiu de 4 issues para **0**.

### 2.2 Erro de banco devolvia 401

O `auth-service` tratava qualquer erro da consulta como "chave inválida" (401). Um banco
fora do ar aparecia como erro do cliente, e **nenhum alerta de 5xx dispararia**. Agora
`sql.ErrNoRows` continua 401 e qualquer outro erro devolve 500.

### 2.3 Segredos fora do Git

Chaves do Datadog, PagerDuty, Discord e a senha do Grafana ficam em Secrets criados pelo
`fase4-secrets.ps1` a partir de um `fase4.env` local (protegido por `.gitignore`). O
repositório não contém nenhuma credencial; os contact points usam `${VAR}` resolvido em
tempo de execução pelo Grafana.

### 2.4 Menor privilégio no self-healer

A automação só tem `get` e `patch` em `deployments`, e apenas no namespace `toogle-master`
(Role + RoleBinding). Ela roda como usuário não-root, com `readOnlyRootFilesystem` e todas
as capabilities removidas.

---

## 3. Infraestrutura (Terraform)

| Problema | Causa | Solução |
|---|---|---|
| Pods não conectavam no RDS/Redis (timeout) | Os nós do node group gerenciado recebem a **SG criada pelo EKS** (`eks-cluster-sg-*`), e não a `nodes` deste projeto. Como o pod usa o ENI do nó (VPC CNI), a origem real do tráfego não estava liberada | Duas `aws_security_group_rule` novas liberando 5432 e 6379 a partir de `module.eks.cluster_security_group_id` |
| Stack de observabilidade não cabia no cluster | `t3.medium` tem 4 GiB e limite de 17 pods por nó | Node group passou para **t3.large** (8 GiB, até 35 pods) |
| `terraform init` falhava com `use_lockfile` | `required_version = ">= 1.9.0"`, mas o lock nativo do S3 exige **1.10+** | `required_version = ">= 1.10.0"` |
| Bucket do state inacessível | O AWS Academy troca a conta entre sessões (o bucket e o ECR ficam na conta antiga) | Procedimento de troca de conta documentado no `COMO-SUBIR-FASE4.md` |
| `bootstrap` falha no refresh | SCP do Academy bloqueia `s3:GetBucketObjectLockConfiguration` | O bucket é criado mesmo assim; versionamento, criptografia e bloqueio de acesso público são aplicados via AWS CLI (documentado) |

**Evidência:** `Apply complete! Resources: 37 added, 0 changed, 0 destroyed.`

---

## 4. Base de dados e dados de demonstração

Nenhum script do projeto criava os bancos (`auth_db`, `flag_db`, `targeting_db`), as
tabelas, a API key ou os Secrets das aplicações: os pods subiam sem conseguir funcionar.

O `fase4-secrets.ps1` passou a fazer isso de forma idempotente, com pods `psql`
temporários **dentro do cluster** (o RDS é privado e só aceita conexão vinda dos nós):
cria os bancos, aplica o schema, insere flags e regras de demonstração, gera a
`SERVICE_API_KEY` (guardando apenas o hash SHA-256 no banco) e a `MASTER_KEY`, e cria os
8 Secrets do cluster.

---

## 5. Observabilidade: decisões que evitaram problemas

- **Collector como DaemonSet com `internalTrafficPolicy: Local`**: cada aplicação fala com
  o Collector do próprio nó, o que permite ao `k8sattributes` identificar o pod de origem
  pelo IP. Com um Service comum, o tráfego cairia em outro nó e os metadados se perderiam.
- **Connector `spanmetrics`**: as métricas RED nascem dos spans, iguais para Go e Python.
  Sem isso, o alerta dependeria de nomes de métrica que variam conforme a convenção
  semântica de cada biblioteca de instrumentação.
- **`ignoreDifferences` do `restartedAt`**: sem isso, o `selfHeal` do ArgoCD desfaria o
  `rollout restart` do self-healer, gerando um segundo rollout e ruído de OutOfSync. Nos
  serviços com HPA, o `replicas` também é ignorado, para o Git não brigar com o autoscaler.
- **Admission webhooks do Prometheus Operator desligados**: o Job que aplica o `caBundle`
  depois do deploy aparece como drift para o ArgoCD, que o desfaz em loop.
- **Alert fatigue**: o alerta usa taxa (não contagem), exige 1 minuto acima do limite,
  agrupa por serviço, repete no máximo a cada hora e trata ausência de dados como normal.
  O self-healer tem allowlist e cooldown de 5 minutos, para não virar "restart em loop".

---

## 6. Correções encontradas durante a validação no cluster real

| Problema | Causa | Correção |
|---|---|---|
| `analytics-service` em CrashLoopBackOff | O `telemetry.py` importava o instrumentor de `requests`, biblioteca que esse serviço não usa (`ModuleNotFoundError`) | Instrumentação do `requests` passou a ser opcional (`try/except ImportError`) |
| Mensagem do alerta chegava literal no Discord e no PagerDuty: `{{ printf "%.1f" $$values.B.Value }}` | O `$$` foi usado para escapar a interpolação de variáveis de ambiente, mas neste arquivo só `${VAR}` é interpolado | Templates voltaram a usar `$values` |

**Evidência da correção:** a mensagem passou a chegar como
`auth-service com 100.0% de respostas 5xx`.

---

## 7. Validação de ponta a ponta (23/09/2026)

| Verificação | Resultado |
|---|---|
| Infraestrutura | 37 recursos criados |
| Pipelines | 5/5 verdes, com push no ECR e commit automático neste repositório |
| ArgoCD | 11 Applications `Synced` |
| Aplicações | 8 pods `Running`; tráfego 100% em HTTP 200 |
| Prometheus | métricas por serviço via spanmetrics (auth 747, evaluation 241, flag 249, targeting 248 chamadas) |
| Loki | logs indexados por `k8s_namespace_name`, `k8s_deployment_name` e `service_name` |
| Datadog | `API key validation successful` no Collector |
| Grafana | dashboard, regra de alerta e contact points provisionados pelo Git |
| **Ciclo completo** | falha injetada → alerta `firing` → PagerDuty + Discord → webhook → `rollout restart` **concluído em 10s** → tráfego de volta a 200 → alerta `inactive` |

Trecho real do log da automação:

```
13:27:30  Webhook recebido do Grafana: status=firing, 1 alerta(s).
13:27:30  RUNBOOK: alerta 'auth-service - taxa de erros 5xx acima de 5%' disparou
          -> kubectl rollout restart deployment/auth-service -n toogle-master
13:27:40  ROLLOUT CONCLUIDO deployment/auth-service em 10s (1/1 pods novos disponíveis)
```

---

## 8. Limitações conhecidas

- **Sem volumes persistentes**: o AWS Academy não tem o driver EBS CSI, então Prometheus e
  Loki usam `emptyDir`. Os dados duram enquanto o cluster existir, o que é suficiente para
  a demonstração.
- **Credenciais temporárias**: as chaves do Lab expiram com a sessão; os Secrets são
  recriados a cada sessão pelo `fase4-secrets.ps1`.
- **`toogle-master-base` fica `Progressing`**: o Ingress não tem um ingress controller
  instalado no cluster (herdado da Fase 3). Não afeta nada da Fase 4, já que o acesso aos
  painéis é feito por `port-forward`.
