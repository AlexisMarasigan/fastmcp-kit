{{/*
Expand the name of the chart.
*/}}
{{- define "mcp-toolkit.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name. Honors `fullnameOverride`.
*/}}
{{- define "mcp-toolkit.fullname" -}}
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
Chart name + version, used as label.
*/}}
{{- define "mcp-toolkit.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "mcp-toolkit.labels" -}}
helm.sh/chart: {{ include "mcp-toolkit.chart" . }}
{{ include "mcp-toolkit.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels — used by Deployment + Service + ServiceMonitor.
*/}}
{{- define "mcp-toolkit.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-toolkit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
