{{/*
MODEL_SUPPORTS arrives in the market's short form (`thinking,tools,vision`),
because that is what the Router app reads out of the manifest; llm-init wants
the supports_* keys. The expansion is the one the market llm-init charts do.
`vision` false drops the capability where the engine would refuse images.
*/}}
{{- define "model.supports" -}}
{{- $keys := list -}}
{{- range splitList "," .supports -}}
{{- $s := trim . -}}
{{- if eq $s "thinking" -}}
{{- $keys = concat $keys (list "supports_reasoning" "supports_reasoning_effort") -}}
{{- else if eq $s "tools" -}}
{{- $keys = concat $keys (list "supports_function_calling" "supports_parallel_function_calling" "supports_tool_choice") -}}
{{- else if eq $s "vision" -}}
{{- if $.vision -}}
{{- $keys = append $keys "supports_vision" -}}
{{- end -}}
{{- else if not (or (eq $s "none") (eq $s "")) -}}
{{- fail (printf "MODEL_SUPPORTS: %q is not one of thinking, tools, vision, none" $s) -}}
{{- end -}}
{{- end -}}
{{- join "," $keys -}}
{{- end -}}
