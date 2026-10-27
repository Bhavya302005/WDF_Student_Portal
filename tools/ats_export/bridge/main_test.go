package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/strelov1/freehire/internal/ingest/sources"
	"golang.org/x/net/html"
)

func exampleJob() sources.Job {
	return sources.Job{ExternalID: "req-1", URL: "https://jobs.example.com/req-1", Title: "Engineer",
		Company: "Acme", Location: "New York, NY, United States", Description: "A full employer description.", Countries: []string{"us"}}
}

type fakeSource struct {
	jobs []sources.Job
	err  error
}

func (f fakeSource) Provider() string { return "workday" }
func (f fakeSource) Fetch(context.Context, sources.CompanyEntry) ([]sources.Job, error) {
	return f.jobs, f.err
}

type hydratingFake struct {
	fakeSource
	t *testing.T
}

func (f hydratingFake) FetchNew(_ context.Context, _ sources.CompanyEntry, seen func(string) bool) ([]sources.Job, error) {
	if !seen("req-1") {
		f.t.Fatal("previously hydrated job was not seen")
	}
	return []sources.Job{{ExternalID: "req-1", SeenRefresh: true}}, nil
}

func TestUSGeographyRequiresEvidence(t *testing.T) {
	for _, tc := range []struct {
		location  string
		countries []string
		want      bool
	}{
		{"Remote", nil, false}, {"Toronto, Canada", nil, false}, {"New York, NY", nil, true},
		{"Remote - US: All locations", nil, true},
		{"United States", []string{"CA"}, false}, {"", []string{"US"}, true},
	} {
		j := exampleJob()
		j.Location, j.Countries = tc.location, tc.countries
		cc, _ := countries(j)
		if got := contains(cc, "us"); got != tc.want {
			t.Errorf("%+v: USA=%v", tc, got)
		}
	}
}

func TestRefreshPreservesDescriptionSalaryAndHydrationTime(t *testing.T) {
	dir := t.TempDir()
	b := board{Provider: "workday", Board: "acme.wd1.myworkdayjobs.com/External", Company: "Acme"}
	j := exampleJob()
	salary := 90000
	j.SalaryMin = &salary
	j.SalaryCurrency = "USD"
	j.SalaryPeriod = "year"
	jobs, first := crawl(context.Background(), b, fakeSource{jobs: []sources.Job{j}}, dir, time.Hour, true)
	if len(jobs) != 1 || first.Status != "ok" {
		t.Fatalf("initial crawl: %+v", first)
	}
	path := filepath.Join(dir, digest(b.key())+".json")
	data, _ := os.ReadFile(path)
	var before boardState
	_ = json.Unmarshal(data, &before)
	jobs, second := crawl(context.Background(), b, hydratingFake{fakeSource: fakeSource{}, t: t}, dir, time.Hour, true)
	if second.Cached != 1 || len(jobs) != 1 || jobs[0].Description != j.Description || *jobs[0].SalaryMin != float64(salary) {
		t.Fatalf("refresh lost content: %+v %+v", jobs, second)
	}
	data, _ = os.ReadFile(path)
	var after boardState
	_ = json.Unmarshal(data, &after)
	if !before.Jobs["req-1"].HydratedAt.Equal(after.Jobs["req-1"].HydratedAt) {
		t.Fatal("refresh must not extend detail TTL")
	}
}

func TestFailureAndUnreadableCannotProveClosure(t *testing.T) {
	b := board{Provider: "workday", Board: "acme.wd1.myworkdayjobs.com/External", Company: "Acme"}
	dir := t.TempDir()
	_, r := crawl(context.Background(), b, fakeSource{err: errors.New("429")}, dir, time.Hour, true)
	if r.Status != "failed" || r.ListingComplete {
		t.Fatalf("%+v", r)
	}
	_, r = crawl(context.Background(), b, fakeSource{jobs: []sources.Job{{ExternalID: "1", Unreadable: true}}}, dir, time.Hour, true)
	if r.Status != "partial" || r.ListingComplete {
		t.Fatalf("%+v", r)
	}
	jobs, r := crawl(context.Background(), b, fakeSource{jobs: []sources.Job{exampleJob()}, err: context.DeadlineExceeded}, dir, time.Hour, true)
	if len(jobs) != 1 || r.Status != "partial" || r.ListingComplete {
		t.Fatalf("verified partial work was lost: %+v", r)
	}
}

func TestDedupePreservesSeparateRequisitionsWithSameTitle(t *testing.T) {
	a := outputJob{URL: "https://jobs.example.com/1", Title: "Engineer", Company: "Acme"}
	b := a
	b.URL = "https://jobs.example.com/2"
	c := a
	c.URL += "#apply"
	jobs, duplicates := dedupe([]outputJob{a, b, c})
	if len(jobs) != 2 || duplicates != 1 {
		t.Fatalf("jobs=%d duplicates=%d", len(jobs), duplicates)
	}
}

func TestRealURLRecognitionAndAliasDedupe(t *testing.T) {
	items := []candidate{
		{URL: "https://jobs.lever.co/acme/req1", Company: "Acme"},
		{URL: "https://jobs.lever.co/acme/req2", Company: "Acme"},
		{URL: "https://careers-acme.icims.com/jobs/1/role"},
		{URL: "https://jobs.lever.co.evil.example/acme/req1"},
		{URL: "https://example.com/?url=https://jobs.lever.co/acme/req1"},
	}
	boards := detectCandidates(items)
	if len(boards) != 2 {
		t.Fatalf("wanted two boards, got %+v", boards)
	}
	for _, b := range boards {
		if b.Status != "pending" {
			t.Fatal("unverified board marked active")
		}
	}
}

func TestStructuredSalaryIsForwardedWithoutBudgetGuessing(t *testing.T) {
	j := exampleJob()
	j.Description = "Experience managing $100M+ capital projects"
	got := mapJob(board{Provider: "workday"}, j, []string{"us"}, "structured")
	if _, exists := got.Structured["salary"]; exists || got.SalaryMin != nil {
		t.Fatal("JD budget turned into structured salary")
	}
}

func TestSuccessFactorsLocationComesOnlyFromExplicitLabel(t *testing.T) {
	root, _ := html.Parse(strings.NewReader(`<p>We have offices in the United States and Canada.</p><span class="joblayouttoken-label">Job Location:&nbsp;</span><span><p>Winsted, Minnesota United States<br/></p></span>`))
	if got := explicitWorkplace(root); got != "Winsted, Minnesota United States" {
		t.Fatal(got)
	}
	root, _ = html.Parse(strings.NewReader(`<p>We have offices in the United States.</p>`))
	if got := explicitWorkplace(root); got != "" {
		t.Fatal("inferred workplace from company boilerplate")
	}
	root, _ = html.Parse(strings.NewReader(`<span itemprop="jobLocation"><span itemprop="address"><meta itemprop="addressLocality" content="Madison"><meta itemprop="addressRegion" content="IN"><meta itemprop="addressCountry" content="US"></span></span>`))
	if got := explicitWorkplace(root); got != "Madison, IN, US" {
		t.Fatal(got)
	}
}

type recordingHTTP struct {
	sources.HTTPClient
	got string
}

func (r *recordingHTTP) GetHTML(_ context.Context, raw string) (*html.Node, error) {
	r.got = raw
	return html.Parse(strings.NewReader(`<html><body>Listing</body></html>`))
}

func TestJobviteUsesPublicNoLayoutViewAndPreservesOtherParameters(t *testing.T) {
	transport := &recordingHTTP{}
	client := &locationClient{HTTPClient: transport}
	_, err := client.GetHTML(context.Background(), "https://jobs.jobvite.com/acme/jobs?department=Engineering")
	if err != nil {
		t.Fatal(err)
	}
	if transport.got != "https://jobs.jobvite.com/acme/jobs?department=Engineering&nl=1" {
		t.Fatal(transport.got)
	}
}

func TestDecimalHourlySalarySurvivesUpstreamIntegerSchema(t *testing.T) {
	client := &locationClient{}
	client.rememberSalaries("api.lever.co", []byte(`[{"hostedUrl":"https://jobs.lever.co/acme/1","salaryRange":{"min":30.72,"max":35.29,"currency":"USD","interval":"per-hour-wage"}}]`))
	job := outputJob{URL: "https://jobs.lever.co/acme/1", Structured: map[string]any{}}
	applyPreciseSalary(&job, client)
	if job.SalaryMin == nil || *job.SalaryMin != 30.72 || *job.SalaryMax != 35.29 || job.SalaryPeriod != "hour" {
		t.Fatalf("salary precision lost: %+v", job)
	}
}
