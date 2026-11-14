// Lightweight completion provider for the Lambda language.
//
// This is deliberately not a language server: it reads the generated
// language-data.json plus definitions in the current document and returns
// plain completion items.  Keeping it dependency-free means the extension can
// be copied into ~/.vscode/extensions without a build step.

const vscode = require("vscode");
const fs = require("fs");
const path = require("path");

let DATA = null;
let PI_DATA = null;

const NAME =
  "[A-Za-z_][A-Za-z0-9_?!<>-]*(?:::[A-Za-z_][A-Za-z0-9_?!<>-]*)*";

function loadData(context) {
  try {
    const file = path.join(context.extensionPath, "language-data.json");
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (err) {
    console.warn("lambda: could not load language-data.json:", err.message);
    return {
      keywords: [], types: [], constants: [], primitives: [], operators: [],
    };
  }
}

function loadPiData(context) {
  try {
    const file = path.join(context.extensionPath, "pi-language-data.json");
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (err) {
    console.warn("pi: could not load pi-language-data.json:", err.message);
    return { keywords: [], types: [], constants: [], builtins: [], attributes: [] };
  }
}

function add(items, seen, label, kind, detail, rank) {
  if (!label || seen.has(label)) return;
  seen.add(label);
  const item = new vscode.CompletionItem(label, kind);
  item.detail = detail;
  item.insertText = label;
  item.sortText = rank + label;
  items.push(item);
}

function provideCompletions(document) {
  const data = DATA || {};
  const text = document.getText();
  const items = [];
  const seen = new Set();

  // Locally defined values and functions: `name → ...` / `name -> ...`.
  for (const m of text.matchAll(
      new RegExp(`(?:^|\\n)\\s*(${NAME})\\s*(?:→|->)`, "g"))) {
    add(items, seen, m[1], vscode.CompletionItemKind.Function,
        "definition in this file", "0_");
  }
  // Local type declarations and sum variants.
  for (const m of text.matchAll(
      new RegExp(`\\b(?:type|enum)\\s+(${NAME})`, "g"))) {
    add(items, seen, m[1], vscode.CompletionItemKind.Class,
        "type in this file", "1_");
  }
  for (const m of text.matchAll(
      new RegExp(`(?:^|\\n)\\s*\\|\\s*(${NAME})`, "g"))) {
    add(items, seen, m[1], vscode.CompletionItemKind.EnumMember,
        "sum variant", "1_");
  }
  // Record/sum field names (`re: Float`, `value: a`).
  for (const m of text.matchAll(
      new RegExp(`(${NAME})\\s*:`, "g"))) {
    add(items, seen, m[1], vscode.CompletionItemKind.Field,
        "field name", "1_");
  }

  for (const name of data.primitives || []) {
    add(items, seen, name, vscode.CompletionItemKind.Function,
        "Lambda primitive", "2_");
  }
  for (const name of data.keywords || []) {
    add(items, seen, name, vscode.CompletionItemKind.Keyword,
        "keyword", "3_");
  }
  for (const name of data.types || []) {
    add(items, seen, name, vscode.CompletionItemKind.Class,
        "built-in type", "4_");
  }
  for (const name of data.constants || []) {
    add(items, seen, name, vscode.CompletionItemKind.Constant,
        "constant", "5_");
  }
  for (const name of data.operators || []) {
    add(items, seen, name, vscode.CompletionItemKind.Operator,
        "operator", "6_");
  }
  return new vscode.CompletionList(items, false);
}

function providePiCompletions(document) {
  const data = PI_DATA || {};
  const text = document.getText();
  const items = [];
  const seen = new Set();
  const nameRe = /(?:^|\n)\s*([a-z][A-Za-z0-9_']*)\s*(?:→|->|:=)/g;
  for (const m of text.matchAll(nameRe)) {
    add(items, seen, m[1], vscode.CompletionItemKind.Function,
        "definition in this file", "0_");
  }
  for (const m of text.matchAll(/\b(?:protocol|elem|system|program)\s+([A-Z][A-Za-z0-9_']*)/g)) {
    add(items, seen, m[1], vscode.CompletionItemKind.Class,
        "Pi declaration", "1_");
  }
  for (const name of data.keywords || []) {
    add(items, seen, name, vscode.CompletionItemKind.Keyword, "Pi keyword", "2_");
  }
  for (const name of data.builtins || []) {
    add(items, seen, name, vscode.CompletionItemKind.Function, "Pi builtin", "3_");
  }
  for (const name of data.types || []) {
    add(items, seen, name, vscode.CompletionItemKind.Class, "Pi type", "4_");
  }
  for (const name of data.constants || []) {
    add(items, seen, name, vscode.CompletionItemKind.Constant, "Pi constant", "5_");
  }
  for (const name of data.attributes || []) {
    add(items, seen, "@" + name, vscode.CompletionItemKind.Keyword, "scheduler attribute", "6_");
  }
  return new vscode.CompletionList(items, false);
}

function activate(context) {
  DATA = loadData(context);
  PI_DATA = loadPiData(context);
  context.subscriptions.push(
    vscode.languages.registerCompletionItemProvider("lambda", {
      provideCompletionItems(document) {
        return provideCompletions(document);
      },
    }),
    vscode.languages.registerCompletionItemProvider("pi", {
      provideCompletionItems(document) {
        return providePiCompletions(document);
      },
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
