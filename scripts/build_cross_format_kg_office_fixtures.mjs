#!/usr/bin/env node
/** Build XLSX and PPTX fixtures for the cross-format knowledge-graph evaluation. */

import fs from "node:fs/promises";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";


const argv = process.argv.slice(2);

function option(name, { required = false } = {}) {
  const index = argv.indexOf(name);
  const value = index >= 0 ? argv[index + 1] : null;
  if (required && (!value || value.startsWith("--"))) {
    throw new Error(
      "usage: build_cross_format_kg_office_fixtures.mjs " +
      "--out CORPUS_ROOT --preview-dir PREVIEW_ROOT " +
      "[--only xlsx|pptx|all] [--node-modules DIR]",
    );
  }
  return value;
}

const outputRoot = path.resolve(option("--out", { required: true }));
const previewRoot = path.resolve(option("--preview-dir", { required: true }));
const only = option("--only") ?? "all";
if (!new Set(["xlsx", "pptx", "all"]).has(only)) {
  throw new Error("--only must be one of: xlsx, pptx, all");
}
const nodeModulesArgument = option("--node-modules");
const nodeModulesDir = nodeModulesArgument
  ? path.resolve(nodeModulesArgument)
  : process.env.CODEX_WORKSPACE_NODE_MODULES;
const outputDir = path.join(outputRoot, "project-orion");
const previewDir = path.join(previewRoot, "project-orion");

async function loadArtifactTool() {
  try {
    return await import("@oai/artifact-tool");
  } catch (error) {
    if (!nodeModulesDir) {
      throw new Error(
        "@oai/artifact-tool was not found. Pass --node-modules DIR or set " +
        "CODEX_WORKSPACE_NODE_MODULES.",
        { cause: error },
      );
    }
    const modulePath = path.join(
      nodeModulesDir,
      "@oai",
      "artifact-tool",
      "dist",
      "artifact_tool.mjs",
    );
    return import(pathToFileURL(modulePath).href);
  }
}

async function loadJsZip() {
  try {
    const module = await import("jszip");
    return module.default ?? module;
  } catch (error) {
    if (!nodeModulesDir) {
      throw new Error(
        "jszip was not found. Pass --node-modules DIR or set " +
        "CODEX_WORKSPACE_NODE_MODULES.",
        { cause: error },
      );
    }
    const runtimeRequire = createRequire(
      path.join(path.dirname(nodeModulesDir), "fixture-loader.cjs"),
    );
    return runtimeRequire("jszip");
  }
}

const {
  Presentation,
  PresentationFile,
  SpreadsheetFile,
  Workbook,
} = await loadArtifactTool();
const JSZip = await loadJsZip();

const FILES = {
  assignmentHistory: "02_ORION-27_担当履歴.xlsx",
  planV1: "03_ORION-27_体制計画_v1.pptx",
  planV2: "04_ORION-27_体制計画_v2.pptx",
};

const palette = {
  navy: "#15324B",
  blue: "#2E75B6",
  paleBlue: "#EAF4FB",
  paleGray: "#F5F7F9",
  line: "#CBD6DF",
  ink: "#17212B",
  muted: "#52616F",
  white: "#FFFFFF",
  amber: "#FFF0C2",
  amberInk: "#9A6700",
  green: "#177A5B",
  paleGreen: "#EAF8F3",
};
const FIXTURE_FONT = "Hiragino Sans";
const FIXED_CORE_TIMESTAMP = "2026-01-01T00:00:00Z";
const FIXED_ZIP_DATE = new Date("2000-01-01T00:00:00.000Z");


function compareCodePoints(left, right) {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function readXmlAttribute(tag, attributeName) {
  const pattern = new RegExp(
    `\\s${escapeRegExp(attributeName)}\\s*=\\s*(["'])(.*?)\\1`,
  );
  const match = tag.match(pattern);
  return match ? match[2] : null;
}

function replaceXmlAttribute(tag, attributeName, value) {
  const pattern = new RegExp(
    `(\\s${escapeRegExp(attributeName)}\\s*=\\s*)(["'])(.*?)\\2`,
  );
  if (!pattern.test(tag)) {
    throw new Error(`missing ${attributeName} attribute in XML element: ${tag}`);
  }
  return tag.replace(pattern, (_match, prefix, quote) => {
    return `${prefix}${quote}${value}${quote}`;
  });
}

function deterministicGuid(seed) {
  const bytes = Buffer.from(createHash("sha1").update(seed).digest().subarray(0, 16));
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex").toUpperCase();
  return `{${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-` +
    `${hex.slice(16, 20)}-${hex.slice(20)}}`;
}

function deterministicUInt32(seed) {
  const value = createHash("sha256").update(seed).digest().readUInt32BE(0);
  return String(value === 0 ? 1 : value);
}

function relationshipOwnerPath(relationshipPath) {
  if (relationshipPath === "_rels/.rels") return null;
  const relationshipDir = path.posix.dirname(relationshipPath);
  if (path.posix.basename(relationshipDir) !== "_rels") {
    throw new Error(`unexpected relationships path: ${relationshipPath}`);
  }
  const ownerDir = path.posix.dirname(relationshipDir);
  const ownerName = path.posix.basename(relationshipPath, ".rels");
  return path.posix.join(ownerDir, ownerName);
}

function normalizeRelationships(xml, relationshipPath) {
  const wrapper = /(<Relationships\b[^>]*>)([\s\S]*?)(<\/Relationships>)/.exec(xml);
  if (!wrapper) {
    throw new Error(`invalid relationships XML: ${relationshipPath}`);
  }
  const relationPattern = /<Relationship\b[^>]*\/>/g;
  const tags = [...wrapper[2].matchAll(relationPattern)].map((match, index) => {
    const tag = match[0];
    const oldId = readXmlAttribute(tag, "Id");
    const type = readXmlAttribute(tag, "Type") ?? "";
    const target = readXmlAttribute(tag, "Target") ?? "";
    const targetMode = readXmlAttribute(tag, "TargetMode") ?? "";
    if (!oldId) {
      throw new Error(`relationship without Id in ${relationshipPath}`);
    }
    return {
      oldId,
      tag,
      originalIndex: index,
      sortKey: `${type}\u0000${targetMode}\u0000${target}`,
    };
  });
  const residue = wrapper[2].replace(relationPattern, "").trim();
  if (residue) {
    throw new Error(`unexpected relationships content in ${relationshipPath}`);
  }

  const ordered = [...tags].sort((left, right) => {
    return compareCodePoints(left.sortKey, right.sortKey) ||
      left.originalIndex - right.originalIndex;
  });
  const idMap = new Map();
  const normalizedTags = ordered.map((relationship, index) => {
    const stableId = `rId${index + 1}`;
    idMap.set(relationship.oldId, stableId);
    return replaceXmlAttribute(relationship.tag, "Id", stableId);
  });

  const replacement = `${wrapper[1]}${normalizedTags.join("")}${wrapper[3]}`;
  return {
    idMap,
    xml: xml.slice(0, wrapper.index) + replacement +
      xml.slice(wrapper.index + wrapper[0].length),
  };
}

function replaceRelationshipReferences(xml, idMap) {
  const attributePattern = /(\s[\w:.-]+\s*=\s*)(["'])([^"']*)\2/g;
  return xml.replace(
    attributePattern,
    (match, prefix, quote, value) => idMap.has(value)
      ? `${prefix}${quote}${idMap.get(value)}${quote}`
      : match,
  );
}

function normalizeCreationIds(xml, archiveName, entryName) {
  let a16Index = 0;
  let p14Index = 0;
  let normalized = xml.replace(/<a16:creationId\b[^>]*>/g, (tag) => {
    const value = deterministicGuid(
      `${archiveName}\u0000${entryName}\u0000a16\u0000${a16Index}`,
    );
    a16Index += 1;
    return replaceXmlAttribute(tag, "id", value);
  });
  normalized = normalized.replace(/<p14:creationId\b[^>]*>/g, (tag) => {
    const value = deterministicUInt32(
      `${archiveName}\u0000${entryName}\u0000p14\u0000${p14Index}`,
    );
    p14Index += 1;
    return replaceXmlAttribute(tag, "val", value);
  });
  return normalized;
}

function normalizeCoreTimestamps(xml) {
  let normalized = xml;
  for (const elementName of ["created", "modified"]) {
    const pattern = new RegExp(
      `(<dcterms:${elementName}\\b[^>]*>)[^<]*(<\\/dcterms:${elementName}>)`,
      "g",
    );
    normalized = normalized.replace(
      pattern,
      `$1${FIXED_CORE_TIMESTAMP}$2`,
    );
  }
  return normalized;
}

function relationshipIds(xml) {
  return [...xml.matchAll(/<Relationship\b[^>]*\bId\s*=\s*(["'])(.*?)\1/g)]
    .map((match) => match[2]);
}

function assertRelationshipIntegrity(entries) {
  for (const [entryName, entry] of entries) {
    if (!entryName.endsWith(".rels") || entry.dir) continue;
    const relationshipsXml = entry.text;
    const ids = relationshipIds(relationshipsXml);
    if (new Set(ids).size !== ids.length) {
      throw new Error(`duplicate relationship Id in ${entryName}`);
    }
    const ownerPath = relationshipOwnerPath(entryName);
    if (!ownerPath) continue;
    const owner = entries.get(ownerPath);
    if (!owner || owner.text == null) {
      throw new Error(`missing relationship owner ${ownerPath}`);
    }
    const references = [
      ...owner.text.matchAll(/\br:(?:id|embed|link)\s*=\s*(["'])(.*?)\1/g),
    ].map((match) => match[2]);
    const defined = new Set(ids);
    for (const reference of references) {
      if (!defined.has(reference)) {
        throw new Error(
          `${ownerPath} references undefined relationship ${reference}`,
        );
      }
    }
  }
}

async function normalizeOoxml(archiveBytes, archiveName) {
  const sourceZip = await JSZip.loadAsync(archiveBytes);
  const entries = new Map();

  for (const entryName of Object.keys(sourceZip.files)) {
    const sourceEntry = sourceZip.files[entryName];
    entries.set(entryName, sourceEntry.dir
      ? { dir: true, bytes: null, text: null }
      : { dir: false, bytes: await sourceEntry.async("uint8array"), text: null });
  }

  const relationshipMaps = [];
  for (const [entryName, entry] of entries) {
    if (!entryName.endsWith(".rels") || entry.dir) continue;
    const xml = new TextDecoder("utf-8").decode(entry.bytes);
    const normalized = normalizeRelationships(xml, entryName);
    entry.text = normalized.xml;
    entry.bytes = null;
    relationshipMaps.push({
      ownerPath: relationshipOwnerPath(entryName),
      idMap: normalized.idMap,
    });
  }

  for (const { ownerPath, idMap } of relationshipMaps) {
    if (!ownerPath) continue;
    const owner = entries.get(ownerPath);
    if (!owner || owner.dir) {
      throw new Error(`missing relationship owner ${ownerPath}`);
    }
    const xml = owner.text ?? new TextDecoder("utf-8").decode(owner.bytes);
    owner.text = replaceRelationshipReferences(xml, idMap);
    owner.bytes = null;
  }

  for (const [entryName, entry] of entries) {
    if (entry.dir || (!entryName.endsWith(".xml") && !entryName.endsWith(".rels"))) {
      continue;
    }
    let xml = entry.text ?? new TextDecoder("utf-8").decode(entry.bytes);
    xml = normalizeCreationIds(xml, archiveName, entryName);
    if (entryName === "docProps/core.xml") {
      xml = normalizeCoreTimestamps(xml);
    }
    entry.text = xml;
    entry.bytes = null;
  }

  assertRelationshipIntegrity(entries);

  const normalizedZip = new JSZip();
  const entryNames = [...entries.keys()].sort(compareCodePoints);
  for (const entryName of entryNames) {
    const entry = entries.get(entryName);
    if (entry.dir) {
      normalizedZip.file(entryName, null, {
        createFolders: false,
        date: FIXED_ZIP_DATE,
        dir: true,
      });
      continue;
    }
    normalizedZip.file(entryName, entry.text ?? entry.bytes, {
      binary: entry.text == null,
      compression: "DEFLATE",
      compressionOptions: { level: 9 },
      createFolders: false,
      date: FIXED_ZIP_DATE,
    });
  }
  return normalizedZip.generateAsync({
    type: "uint8array",
    compression: "DEFLATE",
    compressionOptions: { level: 9 },
    platform: "DOS",
    streamFiles: false,
  });
}

async function saveDeterministicOoxml(blob, filePath) {
  const extension = path.extname(filePath);
  const stem = path.basename(filePath, extension);
  const rawPath = path.join(
    previewDir,
    `.${stem}.artifact-tool-raw${extension}`,
  );
  try {
    await blob.save(rawPath);
    const archiveBytes = await fs.readFile(rawPath);
    const bytes = await normalizeOoxml(archiveBytes, path.basename(filePath));
    await fs.writeFile(filePath, bytes);
  } finally {
    await fs.rm(rawPath, { force: true });
    await fs.rm(`${rawPath}.inspect.ndjson`, { force: true });
  }
}


function buildAssignmentHistory() {
  const workbook = Workbook.create();
  const sheet = workbook.worksheets.add("Assignment History");
  sheet.showGridLines = false;

  sheet.getRange("A1:H3").values = [
    [
      "Record ID",
      "Project ID",
      "Work ID",
      "Role",
      "Assignee ID",
      "Valid From",
      "Valid To",
      "Status",
    ],
    [
      "ASG-001",
      "ORION-27",
      "WS-MIG-04",
      "主担当",
      "EMP-104",
      new Date(Date.UTC(2021, 3, 1)),
      new Date(Date.UTC(2023, 2, 31)),
      "FINAL",
    ],
    [
      "ASG-002",
      "ORION-27",
      "WS-MIG-04",
      "主担当",
      "EMP-208",
      new Date(Date.UTC(2023, 3, 1)),
      null,
      "FINAL",
    ],
  ];

  const header = sheet.getRange("A1:H1");
  header.format.fill = palette.blue;
  header.format.font = { name: FIXTURE_FONT, bold: true, color: palette.white };
  header.format.horizontalAlignment = "center";
  header.format.verticalAlignment = "center";
  header.format.wrapText = true;
  header.format.rowHeight = 30;

  const body = sheet.getRange("A2:H3");
  body.format.font = { name: FIXTURE_FONT, color: palette.ink };
  body.format.borders = { preset: "all", style: "thin", color: palette.line };
  body.format.verticalAlignment = "center";
  body.format.rowHeight = 28;
  sheet.getRange("A2:H2").format.fill = palette.white;
  sheet.getRange("A3:H3").format.fill = palette.paleBlue;
  sheet.getRange("F2:G3").format.numberFormat = "yyyy-mm-dd";
  sheet.getRange("F2:G3").format.horizontalAlignment = "center";
  sheet.getRange("A2:E3").format.horizontalAlignment = "left";
  sheet.getRange("H2:H3").format.horizontalAlignment = "center";

  const widths = [16, 18, 18, 14, 18, 16, 16, 14];
  for (let index = 0; index < widths.length; index += 1) {
    sheet.getRangeByIndexes(0, index, 3, 1).format.columnWidth = widths[index];
  }
  sheet.freezePanes.freezeRows(1);
  return workbook;
}

async function saveWorkbook(workbook, filename) {
  await fs.mkdir(outputDir, { recursive: true });
  await fs.mkdir(previewDir, { recursive: true });

  const inspection = await workbook.inspect({
    kind: "sheet,region",
    sheetId: "Assignment History",
    range: "A1:H3",
    maxChars: 6000,
    tableMaxRows: 5,
    tableMaxCols: 10,
    options: { maxResults: 100 },
  });
  console.log(JSON.stringify({ filename, inspection: inspection.ndjson }));

  const formulaErrors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    maxChars: 4000,
  });
  const errorScan = formulaErrors.ndjson ? formulaErrors.ndjson.trim() : "";
  if (errorScan && !errorScan.includes("matched 0 entries")) {
    throw new Error(`${filename}: formula error scan returned ${formulaErrors.ndjson}`);
  }

  const preview = await workbook.render({
    sheetName: "Assignment History",
    autoCrop: "all",
    scale: 1.5,
    format: "png",
  });
  await fs.writeFile(
    path.join(previewDir, `${filename.replace(/\.xlsx$/, "")}-Assignment-History.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );

  const artifact = await SpreadsheetFile.exportXlsx(workbook);
  await saveDeterministicOoxml(artifact, path.join(outputDir, filename));
  await fs.rm(path.join(outputDir, `${filename}.inspect.ndjson`), { force: true });
}


function addText(slide, name, text, position, style) {
  const box = slide.shapes.add({
    geometry: "textbox",
    name,
    position,
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  box.text = text;
  box.text.style = { fontFamily: FIXTURE_FONT, ...style };
  return box;
}

function addBase(slide, eyebrow, title) {
  slide.background.fill = palette.white;
  addText(
    slide,
    "eyebrow",
    eyebrow,
    { left: 72, top: 50, width: 760, height: 28 },
    { fontSize: 16, bold: true, color: palette.blue },
  );
  addText(
    slide,
    "slide-title",
    title,
    { left: 72, top: 92, width: 1136, height: 64 },
    { fontSize: 50, bold: true, color: palette.navy },
  );
}

function setNotes(slide) {
  slide.speakerNotes.textFrame.setText(
    "[Sources]\n- Synthetic evaluation fixture; no external sources.",
  );
}

function styleTable(table, rows, columns) {
  table.styleOptions = { headerRow: true, bandedRows: false };
  table.borders.assign({ style: "solid", fill: palette.line, width: 1 });
  for (let column = 0; column < columns; column += 1) {
    const cell = table.getCell(0, column);
    cell.fill = palette.blue;
    cell.text.style = {
      fontFamily: FIXTURE_FONT,
      fontSize: 18,
      bold: true,
      color: palette.white,
    };
  }
  for (let row = 1; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const cell = table.getCell(row, column);
      cell.fill = row % 2 === 0 ? palette.paleBlue : palette.white;
      cell.text.style = {
        fontFamily: FIXTURE_FONT,
        fontSize: 17,
        color: palette.ink,
      };
    }
  }
}

function addIdentityPanel(slide, { version, status, supersedes = null }) {
  const panel = slide.shapes.add({
    geometry: "rect",
    name: "identity-panel",
    position: { left: 72, top: 224, width: 1136, height: supersedes ? 216 : 176 },
    fill: status === "APPROVED" ? palette.paleGreen : palette.paleGray,
    line: { style: "solid", fill: palette.line, width: 1 },
  });
  panel.text = supersedes
    ? `Project ID: ORION-27\nWork ID: WS-MIG-04\nVersion: ${version}\nStatus: ${status}\nSupersedes: ${supersedes}`
    : `Project ID: ORION-27\nWork ID: WS-MIG-04\nVersion: ${version}\nStatus: ${status}`;
  panel.text.style = {
    fontFamily: FIXTURE_FONT,
    fontSize: 24,
    color: palette.ink,
  };
}

function buildPlan({ approved }) {
  const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
  const version = approved ? "v2" : "v1";
  const status = approved ? "APPROVED" : "DRAFT";
  const assigneeId = approved ? "EMP-208" : "EMP-104";

  const overview = presentation.slides.add();
  addBase(
    overview,
    approved ? "APPROVED v2 / FINAL" : "DRAFT v1 / NOT APPROVED",
    "ORION-27 / 体制計画",
  );
  addIdentityPanel(overview, {
    version,
    status,
    supersedes: approved ? FILES.planV1 : null,
  });
  addText(
    overview,
    "version-note",
    approved
      ? "このv2は上記v1を明示的に置換する承認済み計画です。"
      : "このv1は未承認の提案です。承認済み記録として扱わないでください。",
    { left: 72, top: approved ? 500 : 460, width: 1020, height: 66 },
    {
      fontSize: 22,
      bold: true,
      color: approved ? palette.green : palette.amberInk,
    },
  );
  setNotes(overview);

  const assignment = presentation.slides.add();
  addBase(
    assignment,
    approved ? "APPROVED ASSIGNMENT" : "ASSIGNMENT PROPOSAL",
    approved ? "承認済み担当変更" : "2023-04-01以降の担当案",
  );
  addText(
    assignment,
    "assignment-callout",
    `Assignee ID: ${assigneeId} / Effective: 2023-04-01`,
    { left: 72, top: 178, width: 860, height: 52 },
    {
      fontSize: 26,
      bold: true,
      color: approved ? palette.green : palette.amberInk,
    },
  );

  const values = [
    ["Project ID", "Work ID", "Role", "Assignee ID", "Effective From", "Status"],
    ["ORION-27", "WS-MIG-04", "主担当", assigneeId, "2023-04-01", status],
  ];
  const table = assignment.tables.add({
    rows: 2,
    columns: 6,
    left: 72,
    top: 282,
    width: 1136,
    height: 150,
    columnWidths: [190, 190, 150, 190, 210, 206],
    values,
  });
  styleTable(table, 2, 6);

  if (approved) {
    const reason = assignment.shapes.add({
      geometry: "rect",
      name: "approval-reason",
      position: { left: 72, top: 490, width: 1136, height: 112 },
      fill: palette.paleBlue,
      line: { style: "solid", fill: palette.line, width: 1 },
    });
    reason.text = "変更理由\n本番移行フェーズへの移行に伴う運営体制の再編";
    reason.text.style = {
      fontFamily: FIXTURE_FONT,
      fontSize: 22,
      color: palette.ink,
    };
  } else {
    addText(
      assignment,
      "draft-warning",
      "DRAFT v1：2023-04-01以降もEMP-104を担当とする案。未承認です。",
      { left: 72, top: 502, width: 1000, height: 60 },
      { fontSize: 21, bold: true, color: palette.amberInk },
    );
  }
  setNotes(assignment);
  return presentation;
}

async function writeBlob(filePath, blob) {
  await fs.writeFile(filePath, new Uint8Array(await blob.arrayBuffer()));
}

async function savePresentation(presentation, filename) {
  await fs.mkdir(outputDir, { recursive: true });
  await fs.mkdir(previewDir, { recursive: true });

  const inspection = await presentation.inspect({
    kind: "slide,textbox,shape,table,notes",
    maxChars: 16000,
  });
  console.log(JSON.stringify({ filename, inspection: inspection.ndjson }));

  for (const [index, slide] of presentation.slides.items.entries()) {
    const stem = `${filename.replace(/\.pptx$/, "")}-slide-${index + 1}`;
    const preview = await presentation.export({ slide, format: "png", scale: 1.5 });
    await writeBlob(path.join(previewDir, `${stem}.png`), preview);
    const layout = await slide.export({ format: "layout" });
    await fs.writeFile(path.join(previewDir, `${stem}.layout.json`), await layout.text());
  }

  const deck = await PresentationFile.exportPptx(presentation);
  await saveDeterministicOoxml(deck, path.join(outputDir, filename));
  await fs.rm(path.join(outputDir, `${filename}.inspect.ndjson`), { force: true });
}


if (only === "xlsx" || only === "all") {
  await saveWorkbook(buildAssignmentHistory(), FILES.assignmentHistory);
}
if (only === "pptx" || only === "all") {
  await savePresentation(buildPlan({ approved: false }), FILES.planV1);
  await savePresentation(buildPlan({ approved: true }), FILES.planV2);
}
