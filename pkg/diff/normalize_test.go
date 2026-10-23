package diff

import (
	"testing"

	"github.com/dag-andersen/argocd-diff-preview/pkg/arrayorder"
	"github.com/dag-andersen/argocd-diff-preview/pkg/extract"
	"github.com/dag-andersen/argocd-diff-preview/pkg/git"
	"github.com/dag-andersen/argocd-diff-preview/pkg/matching"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/yaml"
)

func deploymentWithEnv(t *testing.T, env string) unstructured.Unstructured {
	t.Helper()
	obj := map[string]any{}
	require.NoError(t, yaml.Unmarshal([]byte(`
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  namespace: default
spec:
  template:
    spec:
      containers:
      - name: app
        image: nginx
        env:
`+env), &obj))
	return unstructured.Unstructured{Object: obj}
}

func envNames(t *testing.T, m unstructured.Unstructured) []string {
	t.Helper()
	containers, _, err := unstructured.NestedSlice(m.Object, "spec", "template", "spec", "containers")
	require.NoError(t, err)
	var names []string
	for _, e := range containers[0].(map[string]any)["env"].([]any) {
		names = append(names, e.(map[string]any)["name"].(string))
	}
	return names
}

func newApps(m unstructured.Unstructured, branch git.BranchType) []extract.ExtractedApp {
	return []extract.ExtractedApp{{Id: "app", Name: "app", SourcePath: "apps/app.yaml", Branch: branch, Manifests: []unstructured.Unstructured{m}}}
}

func TestSortArraysInApps_DoesNotMutateInput(t *testing.T) {
	rules, err := arrayorder.FromString(".spec.template.spec.containers[*].env")
	require.NoError(t, err)

	apps := newApps(deploymentWithEnv(t, `
        - name: VAR_B
        - name: VAR_A
`), git.Base)

	sorted := sortArraysInApps(apps, rules)

	assert.Equal(t, []string{"VAR_A", "VAR_B"}, envNames(t, sorted[0].Manifests[0]))
	assert.Equal(t, []string{"VAR_B", "VAR_A"}, envNames(t, apps[0].Manifests[0]))
}

func TestIgnoreArrayOrder_EndToEndDiff(t *testing.T) {
	rules, err := arrayorder.FromString(".spec.template.spec.containers[*].env")
	require.NoError(t, err)

	base := deploymentWithEnv(t, `
        - name: B
          value: "2"
        - name: A
          value: "1"
`)
	reordered := deploymentWithEnv(t, `
        - name: A
          value: "1"
        - name: B
          value: "2"
`)
	changed := deploymentWithEnv(t, `
        - name: A
          value: "1"
        - name: B
          value: "3"
`)

	// Without rules, a pure reorder produces a diff
	diffs, err := matching.GenerateAppDiffs(newApps(base, git.Base), newApps(reordered, git.Target), 3, nil, nil)
	require.NoError(t, err)
	assert.Len(t, diffs, 1)

	// With rules, a pure reorder produces no diff
	diffs, err = matching.GenerateAppDiffs(
		sortArraysInApps(newApps(base, git.Base), rules),
		sortArraysInApps(newApps(reordered, git.Target), rules),
		3, nil, nil)
	require.NoError(t, err)
	assert.Empty(t, diffs)

	// Real value changes are still reported
	diffs, err = matching.GenerateAppDiffs(
		sortArraysInApps(newApps(base, git.Base), rules),
		sortArraysInApps(newApps(changed, git.Target), rules),
		3, nil, nil)
	require.NoError(t, err)
	require.Len(t, diffs, 1)
	require.Len(t, diffs[0].Resources, 1)
	assert.Contains(t, diffs[0].Resources[0].Content, `-          value: "2"`)
	assert.Contains(t, diffs[0].Resources[0].Content, `+          value: "3"`)
}
