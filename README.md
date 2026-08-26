# toogle-master-gitops

Repositório de **GitOps** do ToggleMaster: contém *apenas* manifestos
Kubernetes declarativos (nenhum código de aplicação). É o único repositório
que o ArgoCD observa - o cluster nunca recebe `kubectl apply`/`helm upgrade`
manual nem credenciais de push vindas do CI.

## Estrutura

```
toogle-master-gitops/
├── apps/
│   ├── base/                 # Namespace + Ingress compartilhados
│   ├── auth-service/          # ConfigMap, Service, Deployment
│   ├── flag-service/
│   ├── targeting-service/
│   ├── evaluation-service/     # + HPA
│   └── analytics-service/       # + HPA
└── argocd/
    ├── project/toogle-master-project.yaml   # AppProject (escopo/permissões)
    ├── applications/                          # 1 Application por pasta em apps/
    │   ├── base.yaml
    │   ├── auth-service.yaml
    │   ├── flag-service.yaml
    │   ├── targeting-service.yaml
    │   ├── evaluation-service.yaml
    │   └── analytics-service.yaml
    └── root-app.yaml                           # "App of Apps" (bootstrap)
```

## Como funciona o fluxo Pull-Based

1. O pipeline de CI de cada microsserviço builda, escaneia e publica a
   imagem no ECR com a tag `v1.0.0-<commit-hash>`.
2. Como último passo do CI (workflow `update-gitops.yml`, no repositório
   `reusable-workflows`), o `image:` do `apps/<serviço>/deployment.yaml`
   **deste repositório** é atualizado e commitado.
3. O **ArgoCD**, rodando dentro do cluster EKS, está continuamente
   observando (`polling` a cada ~3 min, ou via *webhook* do GitHub) este
   repositório. Ele mesmo detecta a mudança e "puxa" (`pull`) o novo
   manifesto - o CI nunca tem credenciais do cluster, nunca faz `kubectl
   apply` diretamente. Isso é o que caracteriza GitOps *pull-based*
   (em oposição a um CD tradicional que faz *push* das mudanças).
4. Com `syncPolicy.automated` (`prune: true`, `selfHeal: true`), qualquer
   divergência entre o Git e o cluster (inclusive uma alteração manual feita
   via `kubectl edit`) é revertida automaticamente para bater com o Git.

## Bootstrap (rodar uma única vez, após o ArgoCD estar instalado)

```bash
# 1. Aplica o AppProject (define o escopo/permissões do projeto no ArgoCD)
kubectl apply -f argocd/project/toogle-master-project.yaml

# 2. Aplica a Application raiz (App of Apps) - a partir daqui o ArgoCD
#    assume a criação/sync de tudo o mais (base + 5 microsserviços)
kubectl apply -f argocd/root-app.yaml
```

Depois disso, para acompanhar:

```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443
# acesse https://localhost:8080 (login: admin / senha do secret argocd-initial-admin-secret)
argocd app list
```

Você deve ver 6 Applications (`toogle-master-base` + 5 microsserviços), todas
em `Synced`/`Healthy`.

## Por que um repositório separado?

Separar infraestrutura de deploy do código-fonte dos microsserviços é uma
prática central de GitOps: o time de aplicação não precisa (nem deve ter)
acesso de escrita no cluster, e o histórico deste repositório vira a
"fonte da verdade" auditável de tudo que já foi implantado.
