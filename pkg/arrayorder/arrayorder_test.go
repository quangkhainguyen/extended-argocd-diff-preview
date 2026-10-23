package arrayorder

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"sigs.k8s.io/yaml"
)

func parseYAML(t *testing.T, y string) map[string]any {
	t.Helper()
	obj := map[string]any{}
	require.NoError(t, yaml.Unmarshal([]byte(y), &obj))
	return obj
}

func toYAML(t *testing.T, obj map[string]any) string {
	t.Helper()
	b, err := yaml.Marshal(obj)
	require.NoError(t, err)
	return string(b)
}

func mustRules(t *testing.T, s string) []Rule {
	t.Helper()
	rules, err := FromString(s)
	require.NoError(t, err)
	return rules
}

const deployment = `
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      initContainers:
      - name: init
        env:
        - name: VAR_B
        - name: VAR_A
      containers:
      - name: app
        args: ["--z", "--a"]
        env:
        - name: VAR_B
          value: "2"
        - name: VAR_A
          value: "1"
        ports:
        - containerPort: 9090
        - containerPort: 8080
`

func TestFromString(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    []string
		wantErr string
	}{
		{name: "empty", input: "  ", want: nil},
		{name: "single", input: ".spec.containers[*].env", want: []string{".spec.containers[*].env"}},
		{name: "comma and newline separated", input: ".a.b,\n ..env \n,$.c[0].d", want: []string{".a.b", "..env", "$.c[0].d"}},
		{name: "kubectl braces", input: "{.spec.containers[*].env}", want: []string{"{.spec.containers[*].env}"}},
		{name: "no leading dot", input: "spec.volumes", want: []string{"spec.volumes"}},
		{name: "quoted field", input: ".metadata.annotations['a.b/c']", want: []string{".metadata.annotations['a.b/c']"}},
		{name: "unclosed bracket", input: ".spec.containers[*", wantErr: "unclosed"},
		{name: "filter not supported", input: ".spec.containers[?(@.name=='a')]", wantErr: "unsupported bracket"},
		{name: "empty field", input: ".spec..", wantErr: "recursive descent"},
		{name: "missing field name", input: ".spec.[*]", wantErr: "missing field name"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rules, err := FromString(tc.input)
			if tc.wantErr != "" {
				require.Error(t, err)
				assert.Contains(t, err.Error(), tc.wantErr)
				return
			}
			require.NoError(t, err)
			var got []string
			for _, r := range rules {
				got = append(got, r.String())
			}
			assert.Equal(t, tc.want, got)
		})
	}
}

func TestApply_ExactPathOnlySortsMatchedArray(t *testing.T) {
	obj := parseYAML(t, deployment)
	Apply(obj, mustRules(t, ".spec.template.spec.containers[*].env"))

	assert.Equal(t, parseYAML(t, `
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      initContainers:
      - name: init
        env:
        - name: VAR_B
        - name: VAR_A
      containers:
      - name: app
        args: ["--z", "--a"]
        env:
        - name: VAR_A
          value: "1"
        - name: VAR_B
          value: "2"
        ports:
        - containerPort: 9090
        - containerPort: 8080
`), obj)
}

func TestApply_RecursiveDescentAndUnnamedElements(t *testing.T) {
	obj := parseYAML(t, deployment)
	Apply(obj, mustRules(t, "..env, ..containers[0].ports"))

	assert.Equal(t, parseYAML(t, `
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      initContainers:
      - name: init
        env:
        - name: VAR_A
        - name: VAR_B
      containers:
      - name: app
        args: ["--z", "--a"]
        env:
        - name: VAR_A
          value: "1"
        - name: VAR_B
          value: "2"
        ports:
        - containerPort: 8080
        - containerPort: 9090
`), obj)
}

func TestApply_ScalarArrayWildcardAndMissingPath(t *testing.T) {
	obj := parseYAML(t, `
spec:
  groups:
    a: [c, b, a]
    b: [3, 1, 2]
`)
	Apply(obj, mustRules(t, ".spec.groups.*, .does.not.exist[*], .spec.groups.a[5]"))
	assert.Equal(t, "spec:\n  groups:\n    a:\n    - a\n    - b\n    - c\n    b:\n    - 1\n    - 2\n    - 3\n", toYAML(t, obj))
}

func TestApply_IsDeterministicForPermutations(t *testing.T) {
	a := parseYAML(t, `
list:
- {name: x, value: "1"}
- {name: y, valueFrom: {secretKeyRef: {name: s, key: k}}}
- {name: z, value: "3"}
`)
	b := parseYAML(t, `
list:
- {name: z, value: "3"}
- {name: x, value: "1"}
- {name: y, valueFrom: {secretKeyRef: {name: s, key: k}}}
`)
	rules := mustRules(t, ".list")
	Apply(a, rules)
	Apply(b, rules)
	assert.Equal(t, toYAML(t, a), toYAML(t, b))
}
