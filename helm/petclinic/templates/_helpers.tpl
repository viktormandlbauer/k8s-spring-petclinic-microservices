{{- define "petclinic.name" -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- define "petclinic.mysqlSecret" -}}
{{- default (printf "%s-mysql" (include "petclinic.name" .)) .Values.mysql.existingSecret -}}
{{- end -}}
