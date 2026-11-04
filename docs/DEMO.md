# Demo: the commands behind `docs/evidence/`

Run from the repository root with the cluster up (`make cluster images apply`, then the indexer
Job finished). Each block says which evidence file it produces. Times are from this laptop (4 cores,
CPU-only Ollama) and will differ on yours.

```bash
export NS=rahbar
IP=$(minikube ip)                                  # the Ingress answers on http://$IP with Host: rahbar.local
```

## Ask, abstain, cache (`08`, the UI screenshots)

```bash
ask() { curl -s -X POST -H "Host: rahbar.local" -H "content-type: application/json" \
        -d "{\"question\":\"$1\"}" http://$IP/api/ask | python3 -m json.tool; }
ask "What is the minimum attendance required to sit the final exam?"      # answered, with citations
ask "What is the minimum attendance required to sit the final exam?"      # again: "cached": true
ask "How do I get a hostel room and what is the transport timetable and fee?"   # declined
```

An answered response has `answered_from_context: true` and a `citations` list whose every entry matches
a passage in `retrieved`. A declined one has `answered_from_context: false`, no citations, and the
fixed "the documents do not cover that question" text. Open `http://rahbar.local` (add
`<minikube ip> rahbar.local` to the hosts file) for the same thing in the UI.

## Resources, usage, endpoints, HPA (`04` to `07`)

```bash
kubectl get all,pvc,ingress,hpa,networkpolicy,cronjob,job -n $NS
kubectl top pods -n $NS
kubectl get endpoints api -n $NS                   # three pod IPs behind the Service
kubectl get hpa -n $NS                             # a real percentage, not <unknown>
```

## Rollout and rollback (`09`)

```bash
minikube ssh -- "sudo ctr -n k8s.io images tag docker.io/library/rahbar-api:dev docker.io/library/rahbar-api:v2"
kubectl set image deploy/api api=rahbar-api:v2 -n $NS
kubectl rollout status deploy/api -n $NS           # old pods stay ready until new ones are (maxUnavailable 0)
kubectl rollout undo deploy/api -n $NS
kubectl rollout history deploy/api -n $NS
```

## The embedding guardrail (`10`)

```bash
kubectl patch cm rahbar-config -n $NS --type merge -p '{"data":{"EMBED_MODEL":"mxbai-embed-large"}}'
kubectl delete pod -n $NS -l app=api               # restart the API only
kubectl get pods -n $NS -l app=api                 # 0/1 READY, RESTARTS 0
kubectl logs -n $NS deploy/api | grep "pin mismatch"
kubectl patch cm rahbar-config -n $NS --type merge -p '{"data":{"EMBED_MODEL":"nomic-embed-text"}}'
kubectl delete pod -n $NS -l app=api               # back to 3/3
```

## Delete a pod while traffic flows (`11`)

```bash
for i in $(seq 1 40); do curl -s -o /dev/null -w "%{http_code} " -X POST -H "Host: rahbar.local" \
  -H "content-type: application/json" -d '{"question":"What is the hostel curfew?"}' http://$IP/api/ask; done &
kubectl delete pod -n $NS $(kubectl get pod -n $NS -l app=api -o name | head -1)
wait                                               # every status is 200
```

## Scale (`12`) and NetworkPolicy (`13`)

```bash
kubectl scale deploy/api -n $NS --replicas=5 && kubectl get endpoints api -n $NS
UI=$(kubectl get pod -n $NS -l app=ui -o name | head -1)
kubectl exec -n $NS $UI -- curl -s -m 5 http://chroma:8000/api/v2/heartbeat      # times out: blocked
kubectl exec -n $NS $UI -- curl -s -m 5 telnet://redis:6379                      # times out: blocked
API=$(kubectl get pod -n $NS -l app=api -o name | head -1)
kubectl exec -n $NS $API -- python -c "import socket;socket.create_connection(('1.1.1.1',443),5)"  # blocked
```

## Persistence (`14`), metrics and logs (`15`)

```bash
kubectl delete pod chroma-0 -n $NS                 # StatefulSet recreates it on the same PVC
kubectl delete pod -n $NS -l app=redis
kubectl exec -n $NS $API -- python -c "import urllib.request as u;print(u.urlopen('http://localhost:8000/readyz').read())"
kubectl exec -n $NS $API -- python -c "import urllib.request as u;print(u.urlopen('http://localhost:8000/metrics').read().decode())" | grep ^rahbar_
kubectl logs -n $NS $API | grep '"msg": "answered"' | tail -2
```

## Re-index (the CronJob's template, run now) and the hosted LLM

```bash
kubectl create job --from=cronjob/reindex reindex-now -n $NS
kubectl logs -f job/reindex-now -n $NS -c indexer
# A hosted model instead of the laptop's Ollama (the key never goes in a file):
read -rsp "Key: " LLM_API_KEY; echo; export LLM_API_KEY; make secret
kubectl apply -k k8s/overlays/dev-hosted && kubectl rollout restart deploy/api -n $NS
```
