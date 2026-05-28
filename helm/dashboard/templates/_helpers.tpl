{{/*
Expand the name of the chart.
*/}}
{{- define "trendstorm-dashboard.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "trendstorm-dashboard.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "trendstorm-dashboard.labels" -}}
helm.sh/chart: {{ include "trendstorm-dashboard.name" . }}-{{ .Chart.Version | replace "+" "_" }}
{{ include "trendstorm-dashboard.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "trendstorm-dashboard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "trendstorm-dashboard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
