package arrayorder

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// Rule is a parsed JSONPath expression. Every array matched by the path is sorted
// into a canonical order before diffing, so pure reorderings don't show up.
//
// Supported syntax (subset of Kubernetes/kubectl JSONPath):
//
//	.field         child field
//	..field        recursive descent: "field" at any depth
//	.*  / [*]      all map values / all array elements
//	[n]            array element n
//	['a.b']        child field whose name contains special characters
//
// The path may optionally be prefixed with "$" or wrapped in "{...}".
type Rule struct {
	raw      string
	segments []segment
}

type segmentKind int

const (
	segField segmentKind = iota
	segWildcard
	segIndex
	segRecursive
)

type segment struct {
	kind  segmentKind
	name  string
	index int
}

func (r Rule) String() string {
	return r.raw
}

// FromString parses a comma or newline separated list of JSONPath expressions.
func FromString(s string) ([]Rule, error) {
	var rules []Rule
	for _, raw := range strings.FieldsFunc(s, func(r rune) bool { return r == ',' || r == '\n' }) {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			continue
		}
		rule, err := Parse(raw)
		if err != nil {
			return nil, err
		}
		rules = append(rules, rule)
	}
	return rules, nil
}

// Parse parses a single JSONPath expression.
func Parse(raw string) (Rule, error) {
	p := strings.TrimSpace(raw)
	if strings.HasPrefix(p, "{") && strings.HasSuffix(p, "}") {
		p = strings.TrimSpace(p[1 : len(p)-1])
	}
	p = strings.TrimPrefix(p, "$")

	fail := func(msg string) (Rule, error) {
		return Rule{}, fmt.Errorf("invalid JSONPath %q: %s", raw, msg)
	}

	if p == "" {
		return fail("path is empty")
	}
	// Allow paths without leading dot, e.g. "spec.containers"
	if p[0] != '.' && p[0] != '[' {
		p = "." + p
	}

	var segments []segment
	for i := 0; i < len(p); {
		switch {
		case strings.HasPrefix(p[i:], ".."):
			i += 2
			name, next := readName(p, i)
			if name == "" || name == "*" {
				return fail("recursive descent '..' must be followed by a field name")
			}
			segments = append(segments, segment{kind: segRecursive, name: name})
			i = next
		case p[i] == '.':
			i++
			name, next := readName(p, i)
			if name == "" {
				return fail(fmt.Sprintf("missing field name at position %d", i))
			}
			if name == "*" {
				segments = append(segments, segment{kind: segWildcard})
			} else {
				segments = append(segments, segment{kind: segField, name: name})
			}
			i = next
		case p[i] == '[':
			end := strings.IndexByte(p[i:], ']')
			if end < 0 {
				return fail("unclosed '['")
			}
			content := strings.TrimSpace(p[i+1 : i+end])
			i += end + 1
			switch {
			case content == "*":
				segments = append(segments, segment{kind: segWildcard})
			case len(content) >= 2 && (content[0] == '\'' || content[0] == '"') && content[len(content)-1] == content[0]:
				segments = append(segments, segment{kind: segField, name: content[1 : len(content)-1]})
			default:
				n, err := strconv.Atoi(content)
				if err != nil || n < 0 {
					return fail(fmt.Sprintf("unsupported bracket expression [%s] (use [*], [n] or ['field'])", content))
				}
				segments = append(segments, segment{kind: segIndex, index: n})
			}
		default:
			return fail(fmt.Sprintf("unexpected character %q at position %d", p[i], i))
		}
	}

	return Rule{raw: strings.TrimSpace(raw), segments: segments}, nil
}

func readName(p string, start int) (string, int) {
	end := start
	for end < len(p) && p[end] != '.' && p[end] != '[' {
		end++
	}
	return strings.TrimSpace(p[start:end]), end
}

// Apply sorts, in place, every array in obj matched by any of the rules.
func Apply(obj map[string]any, rules []Rule) {
	for _, rule := range rules {
		apply(obj, rule.segments)
	}
}

func apply(node any, segments []segment) {
	if len(segments) == 0 {
		if list, ok := node.([]any); ok {
			sortCanonical(list)
		}
		return
	}

	seg, rest := segments[0], segments[1:]
	switch seg.kind {
	case segField:
		if m, ok := node.(map[string]any); ok {
			if child, ok := m[seg.name]; ok {
				apply(child, rest)
			}
		}
	case segIndex:
		if list, ok := node.([]any); ok && seg.index < len(list) {
			apply(list[seg.index], rest)
		}
	case segWildcard:
		switch v := node.(type) {
		case map[string]any:
			for _, child := range v {
				apply(child, rest)
			}
		case []any:
			for _, child := range v {
				apply(child, rest)
			}
		}
	case segRecursive:
		applyRecursive(node, seg.name, rest)
	}
}

func applyRecursive(node any, name string, rest []segment) {
	switch v := node.(type) {
	case map[string]any:
		if child, ok := v[name]; ok {
			apply(child, rest)
		}
		for _, child := range v {
			applyRecursive(child, name, rest)
		}
	case []any:
		for _, child := range v {
			applyRecursive(child, name, rest)
		}
	}
}

// sortCanonical sorts by the "name" field when every element has one (e.g. env vars,
// ports, volumes), otherwise by the element's JSON representation.
func sortCanonical(list []any) {
	if len(list) < 2 {
		return
	}
	keys := make([]string, len(list))
	allNamed := true
	for i, e := range list {
		if m, ok := e.(map[string]any); ok {
			if name, ok := m["name"].(string); ok {
				keys[i] = name
				continue
			}
		}
		allNamed = false
		break
	}
	if !allNamed {
		for i, e := range list {
			b, err := json.Marshal(e)
			if err != nil {
				return
			}
			keys[i] = string(b)
		}
	}
	sort.Stable(byKey{list: list, keys: keys})
}

type byKey struct {
	list []any
	keys []string
}

func (b byKey) Len() int           { return len(b.list) }
func (b byKey) Less(i, j int) bool { return b.keys[i] < b.keys[j] }
func (b byKey) Swap(i, j int) {
	b.list[i], b.list[j] = b.list[j], b.list[i]
	b.keys[i], b.keys[j] = b.keys[j], b.keys[i]
}
