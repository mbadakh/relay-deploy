{{- define "relay.name" -}}relay{{- end -}}
{{- define "relay.fullname" -}}{{ printf "relay-%s" .Values.customer.slug | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "relay.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: relay
relay.kidum.online/customer: {{ .Values.customer.slug | quote }}
{{- end -}}
{{- define "relay.selectorLabels" -}}
app.kubernetes.io/name: relay
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "keycloak.selectorLabels" -}}
app.kubernetes.io/name: keycloak
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
