{{/*
TrendStorm Helm helper templates.
*/}}

{{/*
Expand the name of the chart.
*/}}
{{- define "trendstorm.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "trendstorm.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label.
*/}}
{{- define "trendstorm.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "trendstorm.labels" -}}
helm.sh/chart: {{ include "trendstorm.chart" . }}
{{ include "trendstorm.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (stable — used by Service selectors and HPA).
*/}}
{{- define "trendstorm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "trendstorm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "trendstorm.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "trendstorm.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference for a component.
Usage: {{ include "trendstorm.image" (dict "registry" .Values.global.imageRegistry "name" .Values.api.image.name "tag" .Values.global.imageTag) }}
*/}}
{{- define "trendstorm.image" -}}
{{- $registry := .registry | default "" }}
{{- $tag := .tag | default "latest" }}
{{- if $registry }}
{{- printf "%s/%s:%s" $registry .name $tag }}
{{- else }}
{{- printf "%s:%s" .name $tag }}
{{- end }}
{{- end }}

{{/*
Common environment variables from global.env values.
*/}}
{{- define "trendstorm.globalEnv" -}}
{{- range $key, $val := .Values.global.env }}
- name: {{ $key }}
  value: {{ $val | quote }}
{{- end }}
{{- end }}

{{/*
Kafka bootstrap env var (shared by all workers).
*/}}
{{- define "trendstorm.kafkaEnv" -}}
- name: KAFKA__BOOTSTRAP_SERVERS
  valueFrom:
    secretKeyRef:
      name: trendstorm-secrets
      key: kafka_bootstrap_servers
{{- end }}

{{/*
Mongo URI env var (shared by all services).
*/}}
{{- define "trendstorm.mongoEnv" -}}
- name: MONGO__URI
  valueFrom:
    secretKeyRef:
      name: trendstorm-secrets
      key: mongo_uri
{{- end }}

{{/*
Linkerd inject annotation — add to pod spec annotations when linkerd.inject=true.
*/}}
{{- define "trendstorm.linkerdAnnotations" -}}
{{- if .Values.linkerd.inject }}
linkerd.io/inject: enabled
{{- end }}
{{- end }}

{{/*
Redis URI env var.
*/}}
{{- define "trendstorm.redisEnv" -}}
- name: REDIS__URL
  valueFrom:
    secretKeyRef:
      name: trendstorm-secrets
      key: redis_url
{{- end }}
