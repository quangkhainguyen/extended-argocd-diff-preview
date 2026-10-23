package matching

import "testing"

// This regression test intentionally fails until order-insensitive env comparison is supported.
func TestArrayOrder_ReversedEnvProducesNoDiff(t *testing.T) {
	base := parseYAML(t, `apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
spec:
  selector:
    matchLabels:
      app: example
  template:
    metadata:
      labels:
        app: example
    spec:
      containers:
      - name: app
        image: nginx:1.27
        env:
        - name: LOG_LEVEL
          value: info
        - name: DB_HOST
          value: postgres
`)
	target := parseYAML(t, `apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
spec:
  selector:
    matchLabels:
      app: example
  template:
    metadata:
      labels:
        app: example
    spec:
      containers:
      - name: app
        image: nginx:1.27
        env:
        - name: DB_HOST
          value: postgres
        - name: LOG_LEVEL
          value: info
`)

	pair := ResourcePair{Base: &base, Target: &target}
	result, err := pair.Diff(5)
	if err != nil {
		t.Fatal(err)
	}
	if result.Content != "" || result.AddedLines != 0 || result.DeletedLines != 0 {
		t.Fatalf("expected no diff for two unchanged env variables in reversed order, got +%d/-%d:\n%s",
			result.AddedLines, result.DeletedLines, result.Content)
	}
}
