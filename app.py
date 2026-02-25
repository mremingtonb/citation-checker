#!/usr/bin/env python3
"""
Citation Checker — Web Application

A Flask web app that lets users upload .docx legal briefs and verifies
case law citations against the CourtListener API in real time.

Usage:
    python app.py
    Then open http://localhost:5000
"""

import csv
import io
import json
import os
import tempfile
import time
import uuid

from dataclasses import asdict
from flask import Flask, request, jsonify, Response, send_file

# Load .env file for local development (ignored in production)
try:
    from dotenv import load_dotenv
    import pathlib
    load_dotenv(pathlib.Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

# Import core logic from the CLI script
from citation_checker import (
    Citation,
    Quote,
    extract_text,
    extract_citations,
    extract_quotes,
    verify_citation,
    verify_quote,
    REQUEST_DELAY,
    compute_ai_score,
    compute_human_error_adjustment,
)
from dataclasses import asdict as _asdict
import requests as http_requests

app = Flask(__name__)

# Store jobs in memory (fine for a single-server deployment)
jobs: dict[str, dict] = {}

COURTLISTENER_TOKEN = os.environ.get("COURTLISTENER_TOKEN", "")

# ---------------------------------------------------------------------------
# HTML Template (embedded to keep it a single file)
# ---------------------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Citation Checker</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #f5f6fa;
    color: #2d3436;
    line-height: 1.6;
  }

  .container {
    max-width: 900px;
    margin: 0 auto;
    padding: 2rem 1.5rem;
  }

  header {
    text-align: center;
    margin-bottom: 2rem;
  }

  header h1 {
    font-size: 1.75rem;
    font-weight: 700;
    color: #1a1a2e;
  }

  header p {
    color: #636e72;
    margin-top: 0.25rem;
    font-size: 0.95rem;
  }

  /* Upload area */
  .upload-area {
    border: 2px dashed #b2bec3;
    border-radius: 12px;
    padding: 3rem 2rem;
    text-align: center;
    background: #fff;
    transition: border-color 0.2s, background 0.2s;
    cursor: pointer;
  }

  .upload-area.dragover {
    border-color: #0984e3;
    background: #edf5ff;
  }

  .upload-area p {
    font-size: 1.05rem;
    color: #636e72;
  }

  .upload-area .icon {
    font-size: 2.5rem;
    margin-bottom: 0.5rem;
  }

  .upload-area input[type="file"] { display: none; }

  .btn {
    display: inline-block;
    padding: 0.6rem 1.5rem;
    border: none;
    border-radius: 8px;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s;
    text-decoration: none;
    color: #fff;
  }

  .btn-primary { background: #0984e3; }
  .btn-primary:hover { background: #0770c2; }
  .btn-secondary { background: #636e72; }
  .btn-secondary:hover { background: #4a5459; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }

  .upload-btn { margin-top: 1rem; }

  /* Progress */
  .progress-section {
    display: none;
    margin-top: 2rem;
    background: #fff;
    border-radius: 12px;
    padding: 1.5rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }

  .progress-bar-outer {
    width: 100%;
    height: 8px;
    background: #dfe6e9;
    border-radius: 4px;
    overflow: hidden;
    margin: 0.75rem 0;
  }

  .progress-bar-inner {
    height: 100%;
    width: 0%;
    background: #0984e3;
    border-radius: 4px;
    transition: width 0.3s ease;
  }

  .progress-text {
    font-size: 0.9rem;
    color: #636e72;
  }

  /* Results table */
  .results-section {
    display: none;
    margin-top: 1.5rem;
  }

  .results-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }

  .results-header h2 {
    font-size: 1.2rem;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }

  th, td {
    text-align: left;
    padding: 0.75rem 1rem;
    font-size: 0.9rem;
  }

  th {
    background: #1a1a2e;
    color: #fff;
    font-weight: 600;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  tr:nth-child(even) td { background: #f8f9fa; }

  tr:hover td { background: #edf5ff; }

  .badge {
    display: inline-block;
    padding: 0.2rem 0.6rem;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .badge-pending    { background: #dfe6e9; color: #636e72; }
  .badge-verified   { background: #d4edda; color: #155724; }
  .badge-mismatch   { background: #fff3cd; color: #856404; }
  .badge-not_found  { background: #f8d7da; color: #721c24; }
  .badge-unrecognized { background: #e2e3e5; color: #383d41; }
  .badge-error      { background: #f8d7da; color: #721c24; }

  .detail-text {
    font-size: 0.8rem;
    color: #636e72;
    margin-top: 0.25rem;
  }

  /* Summary */
  .summary {
    display: none;
    margin-top: 1.5rem;
    background: #fff;
    border-radius: 12px;
    padding: 1.5rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }

  .summary h3 { margin-bottom: 0.75rem; font-size: 1.1rem; }

  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.75rem;
  }

  .stat-card {
    text-align: center;
    padding: 1rem;
    border-radius: 8px;
    background: #f8f9fa;
  }

  .stat-card .num {
    font-size: 1.5rem;
    font-weight: 700;
  }

  .stat-card .label {
    font-size: 0.8rem;
    color: #636e72;
    text-transform: uppercase;
  }

  .stat-verified .num { color: #155724; }
  .stat-not_found .num { color: #721c24; }
  .stat-mismatch .num { color: #856404; }

  .warning-banner {
    display: none;
    margin-top: 1rem;
    padding: 1rem;
    background: #f8d7da;
    border: 1px solid #f5c6cb;
    border-radius: 8px;
    color: #721c24;
    font-weight: 600;
  }

  /* Error message */
  .error-msg {
    display: none;
    margin-top: 1rem;
    padding: 1rem;
    background: #f8d7da;
    border: 1px solid #f5c6cb;
    border-radius: 8px;
    color: #721c24;
  }

  .filename-display {
    margin-top: 0.75rem;
    font-weight: 600;
    color: #2d3436;
  }

  /* AI Score Section */
  .ai-score-section {
    display: none;
    margin-top: 1.5rem;
    background: #fff;
    border-radius: 12px;
    padding: 2rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }

  .ai-score-section h3 {
    text-align: center;
    font-size: 1.2rem;
    margin-bottom: 1rem;
  }

  .score-display {
    text-align: center;
    margin: 1.5rem 0;
  }

  .score-number {
    font-size: 4rem;
    font-weight: 800;
    line-height: 1;
  }

  .score-max {
    font-size: 1.1rem;
    color: #636e72;
    font-weight: 600;
    margin-top: 0.25rem;
  }

  .score-label {
    font-size: 1.1rem;
    font-weight: 600;
    margin-top: 0.5rem;
  }

  .score-green { color: #27ae60; }
  .score-yellow { color: #f39c12; }
  .score-orange { color: #e67e22; }
  .score-red { color: #c0392b; }

  .flagged-banner {
    display: none;
    margin: 1rem 0;
    padding: 1rem;
    background: #c0392b;
    color: #fff;
    border-radius: 8px;
    font-weight: 700;
    font-size: 1rem;
    text-align: center;
  }

  .criteria-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 1.5rem;
  }

  .criteria-table th,
  .criteria-table td {
    text-align: left;
    padding: 0.6rem 0.75rem;
    font-size: 0.85rem;
    border-bottom: 1px solid #eee;
  }

  .criteria-table th {
    background: #f8f9fa;
    color: #2d3436;
    font-weight: 600;
    font-size: 0.85rem;
    text-transform: none;
    letter-spacing: 0;
  }

  .criteria-table .pts-cell {
    text-align: center;
    font-weight: 700;
    white-space: nowrap;
  }

  .criteria-table .detail-cell {
    font-size: 0.8rem;
    color: #636e72;
  }

  .pts-zero { color: #27ae60; }
  .pts-some { color: #e67e22; }
  .pts-max { color: #c0392b; }

  .methodology-box {
    margin-top: 1rem;
    padding: 1rem;
    background: #f8f9fa;
    border-radius: 8px;
    font-size: 0.85rem;
    color: #636e72;
    line-height: 1.5;
  }

  .disclaimer-box {
    margin-bottom: 1.5rem;
    padding: 1rem 1.25rem;
    background: #fff9e6;
    border: 1px solid #f0d060;
    border-radius: 8px;
    font-size: 0.88rem;
    color: #5a4e00;
    line-height: 1.6;
  }

  .options-section {
    margin-bottom: 1.5rem;
    background: #fff;
    border-radius: 12px;
    padding: 1.25rem 1.5rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }

  .option-group {
    margin-bottom: 1rem;
  }

  .option-group:last-child {
    margin-bottom: 0;
  }

  .checkbox-label {
    display: flex;
    align-items: flex-start;
    gap: 0.6rem;
    font-size: 0.9rem;
    color: #2d3436;
    line-height: 1.5;
    cursor: pointer;
  }

  .checkbox-label input[type="checkbox"] {
    margin-top: 0.25rem;
    width: 16px;
    height: 16px;
    flex-shrink: 0;
    cursor: pointer;
  }

  .option-intro {
    font-size: 0.9rem;
    color: #2d3436;
    line-height: 1.5;
    margin-bottom: 0.6rem;
  }

  .sub-option {
    margin-left: 1.5rem;
    margin-top: 0.4rem;
  }

  .privacy-notice {
    margin-top: 1.5rem;
    padding: 1rem;
    background: #edf5ff;
    border: 1px solid #b2d4f5;
    border-radius: 8px;
    font-size: 0.85rem;
    color: #2d3436;
    text-align: center;
  }

  /* Quote verification table */
  .quote-section {
    display: none;
    margin-top: 1.5rem;
  }

  .quote-section h2 {
    font-size: 1.2rem;
    margin-bottom: 1rem;
  }

  .quote-text-cell {
    max-width: 350px;
    font-size: 0.8rem;
    color: #2d3436;
    line-height: 1.4;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: pointer;
  }

  .quote-text-cell.expanded {
    white-space: normal;
    overflow: visible;
  }

  .badge-verified       { background: #d4edda; color: #155724; }
  .badge-found_elsewhere { background: #fff3cd; color: #856404; }
  .badge-not_in_case    { background: #ffe0b2; color: #e65100; }

  /* Human Error Section */
  .human-error-section {
    display: none;
    margin-top: 1.5rem;
    background: #fff;
    border-radius: 12px;
    padding: 2rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }

  .human-error-section h3 {
    font-size: 1.2rem;
    margin-bottom: 0.5rem;
  }

  .human-error-intro {
    font-size: 0.88rem;
    color: #636e72;
    margin-bottom: 1rem;
    line-height: 1.5;
  }

  .he-item {
    padding: 0.75rem 1rem;
    border-radius: 8px;
    margin-bottom: 0.5rem;
    font-size: 0.88rem;
    line-height: 1.5;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
  }

  .he-item-human {
    background: #d4edda;
    border: 1px solid #c3e6cb;
    color: #155724;
  }

  .he-item-ai {
    background: #f8d7da;
    border: 1px solid #f5c6cb;
    color: #721c24;
  }

  .he-classification {
    font-weight: 700;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .he-points {
    font-weight: 700;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .adjusted-score-box {
    margin-top: 1.5rem;
    text-align: center;
    padding: 1.5rem;
    background: #f8f9fa;
    border-radius: 8px;
  }

  .adjusted-score-number {
    font-size: 3rem;
    font-weight: 800;
    line-height: 1;
  }

  .adjusted-score-label {
    font-size: 0.95rem;
    color: #636e72;
    margin-top: 0.5rem;
  }

  .site-footer {
    margin-top: 1.5rem;
    padding: 1.5rem 0;
    text-align: center;
    font-size: 0.85rem;
    color: #636e72;
    border-top: 1px solid #dfe6e9;
  }

  .site-footer a {
    color: #0984e3;
    text-decoration: none;
  }

  .site-footer a:hover {
    text-decoration: underline;
  }

  @media (max-width: 600px) {
    .container { padding: 1rem; }
    th, td { padding: 0.5rem; font-size: 0.8rem; }
  }
</style>
</head>
<body>

<div class="container">
  <header>
    <h1>Generated AI Brief Detector</h1>
    <p>This webpage will attempt to determine if a legal brief is AI generated using several criteria.</p>
    <p>Upload a legal brief (.docx or .pdf) to verify whether the brief may be AI generated.</p>
    <p style="font-style: italic; color: #636e72; margin-top: 0.5rem;">Designed by R. Bronson, Esq.</p>
  </header>

  <div class="disclaimer-box">
    <strong>&#9888; Disclaimer:</strong> Use this checker at the beginning of your workflow.
    An indication that a brief may be AI generated does not mean that it is. Rather, use this tool
    to flag the possibility that a brief contains AI generated content and double-check that
    conclusion yourself. Similarly, an indication that a brief is not AI generated does not mean
    that it does not, in fact, contain AI generated content. Always rely on your intuition if
    something seems off.
  </div>

  <div class="disclaimer-box">
    <strong>&#9888; Important:</strong> While this page immediately deletes anything uploaded,
    NEVER upload original court product and NEVER upload a brief that is not already available
    to the public.
  </div>

  <!-- Options -->
  <div class="options-section">
    <div class="option-group">
      <label class="checkbox-label">
        <input type="checkbox" id="proSeCheck">
        <span>The checker will attempt to determine if the brief was written by a pro se appellant or appellee. However, if you know that the drafter of the brief is pro se, please check this box.</span>
      </label>
    </div>
    <div class="option-group">
      <p class="option-intro">The checker will assume that the relevant jurisdiction is Florida. However, if you believe that law from other jurisdictions may properly be raised, please check any of the following boxes:</p>
      <label class="checkbox-label sub-option">
        <input type="checkbox" id="otherStateCheck">
        <span>Other state jurisdictions</span>
      </label>
      <label class="checkbox-label sub-option">
        <input type="checkbox" id="federalCheck">
        <span>Federal jurisdictions</span>
      </label>
    </div>
  </div>

  <!-- Upload -->
  <div class="upload-area" id="uploadArea">
    <div class="icon">&#128196;</div>
    <p>Drag &amp; drop a .docx or .pdf file here, or click to browse</p>
    <input type="file" id="fileInput" accept=".docx,.pdf">
    <div class="filename-display" id="filenameDisplay"></div>
    <button class="btn btn-primary upload-btn" id="uploadBtn" disabled>Check Brief</button>
  </div>

  <div class="error-msg" id="errorMsg"></div>

  <!-- Progress -->
  <div class="progress-section" id="progressSection">
    <div class="progress-text" id="progressText">Extracting citations...</div>
    <div class="progress-bar-outer">
      <div class="progress-bar-inner" id="progressBar"></div>
    </div>
  </div>

  <!-- Results -->
  <div class="results-section" id="resultsSection">
    <div class="results-header">
      <h2>Citations Found</h2>
      <button class="btn btn-secondary" id="csvBtn" disabled>Download CSV</button>
    </div>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Citation</th>
          <th>Court &amp; Year</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="resultsBody"></tbody>
    </table>
  </div>

  <!-- Summary -->
  <div class="summary" id="summarySection">
    <h3>Verification Summary</h3>
    <div class="summary-grid">
      <div class="stat-card stat-verified">
        <div class="num" id="statVerified">0</div>
        <div class="label">Verified</div>
      </div>
      <div class="stat-card stat-not_found">
        <div class="num" id="statNotFound">0</div>
        <div class="label">Not Found</div>
      </div>
      <div class="stat-card stat-mismatch">
        <div class="num" id="statMismatch">0</div>
        <div class="label">Mismatch</div>
      </div>
      <div class="stat-card">
        <div class="num" id="statTotal">0</div>
        <div class="label">Total</div>
      </div>
    </div>
    <div class="warning-banner" id="warningBanner"></div>
  </div>

  <!-- AI Detection Score -->
  <div class="ai-score-section" id="aiScoreSection">
    <h3>AI Detection Analysis</h3>
    <div class="score-display">
      <div class="score-number" id="scoreNumber">--</div>
      <div class="score-max">out of 100 points</div>
      <div class="score-label" id="scoreLabel"></div>
    </div>
    <div class="flagged-banner" id="flaggedBanner">
      &#9888; FLAGGED: Fabricated case citations detected &mdash; this brief is presumed AI-generated
    </div>
    <table class="criteria-table">
      <thead>
        <tr>
          <th>Criterion</th>
          <th>Points</th>
          <th>Finding</th>
        </tr>
      </thead>
      <tbody id="criteriaBody"></tbody>
    </table>
    <div class="methodology-box">
      <strong>How this score is calculated:</strong> The score is the sum of points across 13 criteria
      that indicate potential AI generation. Each criterion has a maximum point value based on its
      significance as an AI indicator. Higher total scores indicate greater likelihood that the brief
      was AI-generated. The presence of fabricated (non-existent) case citations automatically flags
      the brief as AI-generated regardless of the total score. A score of 0 means no AI indicators
      were detected.
    </div>
  </div>

  <!-- Quote Verification Results -->
  <div class="quote-section" id="quoteSection">
    <h2>Quotation Verification</h2>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Quoted Text</th>
          <th>Attributed To</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="quoteBody"></tbody>
    </table>
  </div>

  <!-- Potential Human Error Analysis -->
  <div class="human-error-section" id="humanErrorSection">
    <h3>Potential Human Error</h3>
    <p class="human-error-intro">
      The following analysis re-examines flagged items to determine whether they are more likely
      human mistakes (such as typos or misremembered sources) or indicators of AI generation.
      The adjusted score accounts for items that appear to be human error.
    </p>
    <div id="humanErrorItems"></div>
    <div class="adjusted-score-box">
      <div>Base AI Score: <strong id="heBaseScore">--</strong></div>
      <div>Adjustment: <strong id="heAdjustment">--</strong></div>
      <hr style="margin:0.75rem 0; border:none; border-top:1px solid #dfe6e9;">
      <div class="adjusted-score-number" id="heAdjustedScore">--</div>
      <div class="adjusted-score-label">Adjusted AI Detection Score</div>
      <div class="score-label" id="heAdjustedLabel" style="margin-top:0.5rem;"></div>
    </div>
  </div>

  <div class="privacy-notice">
    &#128274; <strong>Privacy:</strong> Uploaded files are processed in real time and immediately deleted from the server.
    No documents, text, or citation data are stored after your analysis is complete.
  </div>

  <footer class="site-footer">
    <p>If you have ideas for how the AI detector could be improved, including additional or reweighed AI generation factors, please contact the developer at <a href="mailto:bronsonr@flcourts.org">bronsonr@flcourts.org</a>.</p>
  </footer>
</div>

<script>
const uploadArea = document.getElementById('uploadArea');
const fileInput = document.getElementById('fileInput');
const uploadBtn = document.getElementById('uploadBtn');
const filenameDisplay = document.getElementById('filenameDisplay');
const errorMsg = document.getElementById('errorMsg');
const progressSection = document.getElementById('progressSection');
const progressText = document.getElementById('progressText');
const progressBar = document.getElementById('progressBar');
const resultsSection = document.getElementById('resultsSection');
const resultsBody = document.getElementById('resultsBody');
const csvBtn = document.getElementById('csvBtn');
const summarySection = document.getElementById('summarySection');
const warningBanner = document.getElementById('warningBanner');

let selectedFile = null;
let currentJobId = null;

// Drag & drop
uploadArea.addEventListener('click', (e) => {
  if (e.target === uploadBtn) return;
  fileInput.click();
});

uploadArea.addEventListener('dragover', (e) => {
  e.preventDefault();
  uploadArea.classList.add('dragover');
});

uploadArea.addEventListener('dragleave', () => {
  uploadArea.classList.remove('dragover');
});

uploadArea.addEventListener('drop', (e) => {
  e.preventDefault();
  uploadArea.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file) selectFile(file);
});

fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) selectFile(fileInput.files[0]);
});

function selectFile(file) {
  const name = file.name.toLowerCase();
  if (!name.endsWith('.docx') && !name.endsWith('.pdf')) {
    showError('Please select a .docx or .pdf file.');
    return;
  }
  selectedFile = file;
  filenameDisplay.textContent = file.name;
  uploadBtn.disabled = false;
  hideError();
}

uploadBtn.addEventListener('click', startCheck);

function showError(msg) {
  errorMsg.textContent = msg;
  errorMsg.style.display = 'block';
}

function hideError() {
  errorMsg.style.display = 'none';
}

function resetUI() {
  resultsBody.innerHTML = '';
  resultsSection.style.display = 'none';
  summarySection.style.display = 'none';
  warningBanner.style.display = 'none';
  progressBar.style.width = '0%';
  csvBtn.disabled = true;
  document.getElementById('aiScoreSection').style.display = 'none';
  document.getElementById('flaggedBanner').style.display = 'none';
  document.getElementById('quoteSection').style.display = 'none';
  document.getElementById('quoteBody').innerHTML = '';
  document.getElementById('humanErrorSection').style.display = 'none';
  document.getElementById('humanErrorItems').innerHTML = '';
}

async function startCheck() {
  if (!selectedFile) return;
  hideError();
  resetUI();
  uploadBtn.disabled = true;

  progressSection.style.display = 'block';
  progressText.textContent = 'Uploading and extracting citations...';

  const formData = new FormData();
  formData.append('file', selectedFile);
  formData.append('pro_se', document.getElementById('proSeCheck').checked ? '1' : '0');
  formData.append('allow_other_state', document.getElementById('otherStateCheck').checked ? '1' : '0');
  formData.append('allow_federal', document.getElementById('federalCheck').checked ? '1' : '0');

  try {
    const resp = await fetch('/upload', { method: 'POST', body: formData });
    const data = await resp.json();

    if (!resp.ok) {
      showError(data.error || 'Upload failed.');
      progressSection.style.display = 'none';
      uploadBtn.disabled = false;
      return;
    }

    currentJobId = data.job_id;
    const citations = data.citations;

    if (citations.length === 0) {
      progressSection.style.display = 'none';
      showError('No case citations found in the document.');
      uploadBtn.disabled = false;
      return;
    }

    // Build results table
    resultsSection.style.display = 'block';
    citations.forEach((cite, i) => {
      const tr = document.createElement('tr');
      tr.id = 'row-' + i;
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td>
          <strong>${escHtml(cite.parties)}</strong><br>
          <span style="color:#636e72">${escHtml(cite.volume)} ${escHtml(cite.reporter)} ${escHtml(cite.page)}</span>
          <div class="detail-text" id="detail-${i}"></div>
        </td>
        <td>${escHtml(cite.court)} ${escHtml(cite.year)}</td>
        <td><span class="badge badge-pending" id="badge-${i}">Pending</span></td>
      `;
      resultsBody.appendChild(tr);
    });

    // Start SSE verification
    progressText.textContent = `Verifying 0 / ${citations.length} citations...`;
    startVerification(currentJobId, citations.length);

  } catch (err) {
    showError('Network error: ' + err.message);
    progressSection.style.display = 'none';
    uploadBtn.disabled = false;
  }
}

function startVerification(jobId, total) {
  const evtSource = new EventSource('/verify/' + jobId);
  let completed = 0;
  let quoteTotal = 0;
  let quotesCompleted = 0;

  const stats = { verified: 0, not_found: 0, mismatch: 0, unrecognized: 0, error: 0 };

  evtSource.onmessage = function(event) {
    const data = JSON.parse(event.data);

    if (data.type === 'result') {
      const idx = data.index;
      const cite = data.citation;

      // Update badge
      const badge = document.getElementById('badge-' + idx);
      badge.className = 'badge badge-' + cite.status;
      badge.textContent = formatStatus(cite.status);

      // Update detail — highlight "Did you mean" suggestions
      const detail = document.getElementById('detail-' + idx);
      const detailText = cite.detail || '';
      if (detailText.includes('Did you mean:')) {
        const parts = detailText.split('Did you mean:');
        detail.innerHTML = escHtml(parts[0]) +
          '<em style="color:#0984e3;font-weight:600">Did you mean:' +
          escHtml(parts[1]) + '</em>';
      } else {
        detail.textContent = detailText;
      }

      // Track stats
      if (stats.hasOwnProperty(cite.status)) stats[cite.status]++;

      completed++;
      const pct = Math.round((completed / total) * 100);
      progressBar.style.width = pct + '%';
      progressText.textContent = `Verifying ${completed} / ${total} citations...`;
    }

    if (data.type === 'quote_phase') {
      quoteTotal = data.total;
      if (quoteTotal > 0) {
        document.getElementById('quoteSection').style.display = 'block';
        progressText.textContent = `Verifying 0 / ${quoteTotal} quotations...`;
        progressBar.style.width = '0%';
      }
    }

    if (data.type === 'quote_result') {
      const q = data.quote;
      const qBody = document.getElementById('quoteBody');
      const tr = document.createElement('tr');

      const statusLabels = {
        verified: 'Verified',
        found_elsewhere: 'Found Elsewhere',
        not_found: 'Not Found',
        pending: 'Pending',
      };

      tr.innerHTML =
        '<td>' + (data.index + 1) + '</td>' +
        '<td class="quote-text-cell" onclick="this.classList.toggle(&#39;expanded&#39;)" title="Click to expand">' +
          escHtml(q.text.substring(0, 120)) + (q.text.length > 120 ? '...' : '') +
        '</td>' +
        '<td><span style="color:#636e72;font-size:0.85rem">' + escHtml(q.cite_label) + '</span></td>' +
        '<td><span class="badge badge-' + q.status + '">' + (statusLabels[q.status] || q.status) + '</span>' +
          '<div class="detail-text">' + escHtml(q.detail) + '</div></td>';
      qBody.appendChild(tr);

      quotesCompleted++;
      if (quoteTotal > 0) {
        const pct = Math.round((quotesCompleted / quoteTotal) * 100);
        progressBar.style.width = pct + '%';
        progressText.textContent = `Verifying ${quotesCompleted} / ${quoteTotal} quotations...`;
      }
    }

    if (data.type === 'done') {
      evtSource.close();
      progressText.textContent = 'Verification complete.';
      uploadBtn.disabled = false;

      // Show summary
      document.getElementById('statVerified').textContent = stats.verified;
      document.getElementById('statNotFound').textContent = stats.not_found;
      document.getElementById('statMismatch').textContent = stats.mismatch;
      document.getElementById('statTotal').textContent = total;
      summarySection.style.display = 'block';

      const suspicious = stats.not_found + stats.mismatch;
      if (suspicious > 0) {
        warningBanner.textContent = suspicious + ' citation(s) could not be verified and may be AI-generated.';
        warningBanner.style.display = 'block';
      }

      // Enable CSV download
      csvBtn.disabled = false;
      csvBtn.onclick = () => {
        window.location.href = '/download/' + jobId;
      };

      // Display AI detection score
      if (data.ai_score) {
        displayAiScore(data.ai_score);
      }

      // Display Human Error analysis
      if (data.human_error && data.human_error.items && data.human_error.items.length > 0) {
        displayHumanError(data.human_error, data.ai_score ? data.ai_score.total_score : 0);
      }
    }
  };

  evtSource.onerror = function() {
    evtSource.close();
    progressText.textContent = 'Connection lost. Partial results shown above.';
    uploadBtn.disabled = false;
  };
}

function formatStatus(s) {
  const labels = {
    verified: 'Verified',
    mismatch: 'Name Mismatch',
    not_found: 'Not Found',
    unrecognized: 'Unknown Reporter',
    error: 'Error',
    pending: 'Pending',
  };
  return labels[s] || s;
}

function displayAiScore(aiScore) {
  const section = document.getElementById('aiScoreSection');
  const scoreNum = document.getElementById('scoreNumber');
  const scoreLabel = document.getElementById('scoreLabel');
  const flagged = document.getElementById('flaggedBanner');
  const body = document.getElementById('criteriaBody');

  section.style.display = 'block';

  const score = aiScore.total_score;
  scoreNum.textContent = score;

  let colorClass;
  if (score === 0) colorClass = 'score-green';
  else if (score <= 10) colorClass = 'score-green';
  else if (score <= 30) colorClass = 'score-yellow';
  else if (score <= 50) colorClass = 'score-orange';
  else colorClass = 'score-red';

  scoreNum.className = 'score-number ' + colorClass;
  scoreLabel.textContent = aiScore.label;
  scoreLabel.className = 'score-label ' + colorClass;

  if (aiScore.auto_flagged) {
    flagged.style.display = 'block';
  }

  body.innerHTML = '';
  aiScore.criteria.forEach(function(c) {
    const tr = document.createElement('tr');
    let ptsClass = c.points === 0 ? 'pts-zero' : (c.points >= c.max ? 'pts-max' : 'pts-some');
    tr.innerHTML =
      '<td><strong>' + escHtml(c.name) + '</strong><br>' +
      '<span style="font-size:0.8rem;color:#636e72">' + escHtml(c.description) + '</span></td>' +
      '<td class="pts-cell"><span class="' + ptsClass + '">' + c.points + '</span> / ' + c.max + '</td>' +
      '<td class="detail-cell">' + escHtml(c.detail) + '</td>';
    body.appendChild(tr);
  });

  // Scroll to score section
  section.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function displayHumanError(he, baseScore) {
  const section = document.getElementById('humanErrorSection');
  const itemsDiv = document.getElementById('humanErrorItems');
  section.style.display = 'block';

  itemsDiv.innerHTML = '';
  he.items.forEach(function(item) {
    const isHuman = item.classification === 'human_error';
    const div = document.createElement('div');
    div.className = 'he-item ' + (isHuman ? 'he-item-human' : 'he-item-ai');
    const pointsText = item.points < 0 ? item.points + ' pts' : (item.points > 0 ? '+' + item.points + ' pts' : '—');
    div.innerHTML =
      '<div>' +
        '<span class="he-classification">' + (isHuman ? '&#10003; Likely Human Error' : '&#9888; AI Indicator') + '</span><br>' +
        escHtml(item.description) +
      '</div>' +
      '<div class="he-points">' + pointsText + '</div>';
    itemsDiv.appendChild(div);
  });

  // Calculate adjusted score
  const adjustment = he.adjustment;
  const adjusted = Math.max(0, Math.min(100, baseScore + adjustment));

  document.getElementById('heBaseScore').textContent = baseScore;
  document.getElementById('heAdjustment').textContent = (adjustment >= 0 ? '+' : '') + adjustment;

  const adjScoreEl = document.getElementById('heAdjustedScore');
  adjScoreEl.textContent = adjusted;

  let colorClass;
  if (adjusted === 0) colorClass = 'score-green';
  else if (adjusted <= 10) colorClass = 'score-green';
  else if (adjusted <= 30) colorClass = 'score-yellow';
  else if (adjusted <= 50) colorClass = 'score-orange';
  else colorClass = 'score-red';
  adjScoreEl.className = 'adjusted-score-number ' + colorClass;

  // Adjusted label
  const labelEl = document.getElementById('heAdjustedLabel');
  let label;
  if (adjusted === 0) label = 'Not AI generated';
  else if (adjusted <= 10) label = 'Low chance of AI generation';
  else if (adjusted <= 30) label = 'Moderate chance of some AI generation';
  else if (adjusted <= 50) label = 'High chance of some AI generation';
  else if (adjusted <= 80) label = 'Moderate chance that entire brief was AI generated';
  else label = 'High chance that entire brief was AI generated';
  labelEl.textContent = label;
  labelEl.className = 'score-label ' + colorClass;
}

function escHtml(str) {
  const d = document.createElement('div');
  d.textContent = str || '';
  return d.innerHTML;
}
</script>

</body>
</html>
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return HTML_PAGE


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files["file"]
    fname = file.filename.lower()
    if not (fname.endswith(".docx") or fname.endswith(".pdf")):
        return jsonify({"error": "Please upload a .docx or .pdf file."}), 400

    suffix = ".pdf" if fname.endswith(".pdf") else ".docx"
    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        file.save(tmp.name)
        tmp.close()

        # Extract text and parse citations
        text = extract_text(tmp.name)
        citations = extract_citations(text)
    except Exception as e:
        return jsonify({"error": f"Failed to process file: {e}"}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # Read user-provided options
    pro_se_manual = request.form.get("pro_se") == "1"
    allow_other_state = request.form.get("allow_other_state") == "1"
    allow_federal = request.form.get("allow_federal") == "1"

    # Extract quotes attributed to citations
    quotes = extract_quotes(text, citations)

    # Create a job
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "citations": citations,
        "results": [],
        "quotes": quotes,
        "quote_results": [],
        "text": text,
        "pro_se_manual": pro_se_manual,
        "allow_other_state": allow_other_state,
        "allow_federal": allow_federal,
    }

    # Return the extracted citations (without verification yet)
    return jsonify({
        "job_id": job_id,
        "citations": [
            {
                "parties": c.parties,
                "volume": c.volume,
                "reporter": c.reporter,
                "page": c.page,
                "court": c.court,
                "year": c.year,
            }
            for c in citations
        ],
    })


@app.route("/verify/<job_id>")
def verify(job_id):
    if job_id not in jobs:
        return "Job not found", 404

    def generate():
        job = jobs[job_id]
        citations = job["citations"]

        session = http_requests.Session()
        session.headers.update({
            "Authorization": f"Token {COURTLISTENER_TOKEN}",
        })

        for i, cite in enumerate(citations):
            verify_citation(cite, session)
            job["results"].append(cite)

            payload = json.dumps({
                "type": "result",
                "index": i,
                "citation": {
                    "parties": cite.parties,
                    "volume": cite.volume,
                    "reporter": cite.reporter,
                    "page": cite.page,
                    "court": cite.court,
                    "year": cite.year,
                    "status": cite.status,
                    "matched_case_name": cite.matched_case_name,
                    "detail": cite.detail,
                },
            })
            yield f"data: {payload}\n\n"

            # Rate limiting between requests
            if i < len(citations) - 1:
                time.sleep(REQUEST_DELAY)

        # Compute AI detection score after all citations verified
        ai_result = compute_ai_score(
            job.get("text", ""),
            list(job["results"]),
            pro_se_override=job.get("pro_se_manual", False),
            allow_other_state=job.get("allow_other_state", False),
            allow_federal=job.get("allow_federal", False),
        )

        # --- Phase 2: Quotation verification ---
        quotes = job.get("quotes", [])
        if quotes:
            quote_phase = json.dumps({"type": "quote_phase", "total": len(quotes)})
            yield f"data: {quote_phase}\n\n"

            for qi, quote in enumerate(quotes):
                cite = citations[quote.cite_index] if quote.cite_index < len(citations) else None
                if cite:
                    verify_quote(quote, cite, session)
                else:
                    quote.status = "not_found"
                    quote.detail = "Could not resolve attributed citation"
                job["quote_results"].append(quote)

                q_payload = json.dumps({
                    "type": "quote_result",
                    "index": qi,
                    "quote": {
                        "text": quote.text,
                        "cite_index": quote.cite_index,
                        "cite_label": quote.cite_label,
                        "status": quote.status,
                        "found_in": quote.found_in,
                        "detail": quote.detail,
                    },
                })
                yield f"data: {q_payload}\n\n"

        # Compute human error adjustment
        human_error = compute_human_error_adjustment(
            list(job["results"]),
            list(job.get("quote_results", [])),
        )

        done_payload = {
            "type": "done",
            "ai_score": ai_result,
            "human_error": human_error,
        }
        yield f"data: {json.dumps(done_payload)}\n\n"

        # Privacy: remove stored document text immediately after scoring
        job.pop("text", None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs:
        return "Job not found", 404

    job = jobs[job_id]
    results = job.get("results", [])
    if not results:
        results = job["citations"]

    output = io.StringIO()
    writer = csv.writer(output)

    # Section 1: Citation verification
    writer.writerow([
        "Citation", "Parties", "Volume", "Reporter", "Page",
        "Court", "Year", "Status", "Matched Case Name", "Detail", "Suggestion",
    ])
    for cite in results:
        writer.writerow([
            f"{cite.volume} {cite.reporter} {cite.page}",
            cite.parties,
            cite.volume,
            cite.reporter,
            cite.page,
            cite.court,
            cite.year,
            cite.status,
            cite.matched_case_name,
            cite.detail,
            getattr(cite, "suggestion", ""),
        ])

    # Section 2: Quotation verification (if any)
    quote_results = job.get("quote_results", [])
    if quote_results:
        writer.writerow([])  # Blank line separator
        writer.writerow(["QUOTATION VERIFICATION"])
        writer.writerow([
            "Quoted Text (first 100 chars)", "Attributed Citation",
            "Status", "Found In", "Detail",
        ])
        for q in quote_results:
            writer.writerow([
                q.text[:100] + ("..." if len(q.text) > 100 else ""),
                q.cite_label,
                q.status,
                q.found_in,
                q.detail,
            ])

    buf = io.BytesIO(output.getvalue().encode("utf-8"))
    buf.seek(0)

    # Privacy: remove job data after CSV download
    jobs.pop(job_id, None)

    return send_file(
        buf,
        mimetype="text/csv",
        as_attachment=True,
        download_name="citation_results.csv",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    print("\n  Citation Checker Web App")
    print(f"  Open http://localhost:{port} in your browser\n")
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
