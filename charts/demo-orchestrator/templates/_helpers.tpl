{{- define "demo-orchestrator.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{- define "demo-orchestrator.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "demo-orchestrator.image" -}}
{{ printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}

{{/* Env shared by the operator and the sweeper; paths match the volume mounts below. */}}
{{- define "demo-orchestrator.env" -}}
- name: LEDGER_PATH
  value: /var/lib/orchestrator/ledger.jsonl
- name: PERSONAS_DIR
  value: /etc/orchestrator/personas
- name: MAX_CONCURRENT_ENVS
  value: {{ .Values.config.maxConcurrentEnvs | quote }}
- name: PROVISION_TIMEOUT
  value: {{ .Values.config.provisionTimeout | quote }}
- name: SWEEP_GRACE
  value: {{ .Values.config.sweepGrace | quote }}
- name: BASE_DOMAIN
  value: {{ .Values.config.baseDomain | quote }}
{{- end }}

{{/* Non-root uid of the image; fsGroup makes the ledger volume group-writable for it. */}}
{{- define "demo-orchestrator.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "demo-orchestrator.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}

{{- define "demo-orchestrator.volumeMounts" -}}
- name: ledger
  mountPath: /var/lib/orchestrator
- name: personas
  mountPath: /etc/orchestrator/personas
  readOnly: true
- name: tmp
  mountPath: /tmp
{{- end }}

{{/*
A ConfigMap can't hold subdirectories, so each persona is one key (<name>.yaml)
projected back to <name>/persona.yaml, the layout load_personas() globs.
*/}}
{{- define "demo-orchestrator.volumes" -}}
- name: ledger
  persistentVolumeClaim:
    claimName: {{ .Release.Name }}-ledger
- name: personas
  configMap:
    name: {{ .Release.Name }}-personas
    items:
      {{- range $path, $_ := .Files.Glob "personas/*/persona.yaml" }}
      - key: {{ base (dir $path) }}.yaml
        path: {{ base (dir $path) }}/persona.yaml
      {{- end }}
- name: tmp
  emptyDir: {}
{{- end }}
