// 资料页各接口回包的形状，对着 supervisor/materials_api.py 和 engine/materials 的回包，只认页面用到的字段。
import { BOOLEAN, NON_EMPTY_TEXT, NUMBER, TEXT, at, optional, recordOf, shaped, shapedRows } from '../../api/shape';
import { MEMORY_OWNER, type MaterialScope, type MemoryOwner } from '../../api/supervisorClient';

export type Source = { id: string; name: string; status: string; mime_type: string; warnings?: { message: string }[] };
export type Span = { id: string; text: string; citation: string; locator: { label: string } };
export type Version = { id: string; artifact_id: string; number: number; status: string; sha256: string; page_count: number; format: string };
export type Artifact = { id: string; name: string; current_version: string; versions: Version[] };
export type ArtifactRow = { id: string; name: string };
export type ExtractionField = { id: string; label: string; value: string; uncertain: boolean };
export type Extraction = { id: string; source_id: string; revision: number; status: string; fields: ExtractionField[] };
export type Job = { id: string; status: string; version_id?: string; artifact_id: string; error?: string };
export type JournalHit = { id: string; message_id: string; sender_name: string; timestamp: number; text: string; truncated: boolean };
export type Capabilities = { office_renderer: boolean; modules: Record<string, boolean> };

type Reader<T> = (what: string, value: unknown, path?: string) => T;

/** 整个回包是一列对象时，逐项交给 read，出错能指到第几项。 */
const rowsOf = <T>(read: Reader<T>): Reader<T[]> => (what, value, path = '') => (
  shapedRows(what, value, {}, path).map((row, index) => read(what, row, at(path, index)))
);

const WARNING = { message: TEXT };
const SOURCE = { id: NON_EMPTY_TEXT, name: TEXT, status: TEXT, mime_type: TEXT };

export const readSource: Reader<Source> = (what, value, path = '') => {
  const source = shaped(what, value, SOURCE, path);
  const warnings = source.warnings === undefined ? {} : { warnings: shapedRows(what, source.warnings, WARNING, at(path, 'warnings')) };
  return { id: source.id, name: source.name, status: source.status, mime_type: source.mime_type, ...warnings };
};
export const readSources = rowsOf(readSource);

export const readArtifactRows = rowsOf<ArtifactRow>((what, value, path) => {
  const { id, name } = shaped(what, value, { id: NON_EMPTY_TEXT, name: TEXT }, path);
  return { id, name };
});

const VERSION = { id: NON_EMPTY_TEXT, artifact_id: TEXT, number: NUMBER, status: TEXT, sha256: TEXT, page_count: NUMBER, format: TEXT };

export const readVersion: Reader<Version> = (what, value, path = '') => {
  const { id, artifact_id, number, status, sha256, page_count, format } = shaped(what, value, VERSION, path);
  return { id, artifact_id, number, status, sha256, page_count, format };
};

export const readArtifact: Reader<Artifact> = (what, value, path = '') => {
  const artifact = shaped(what, value, { id: NON_EMPTY_TEXT, name: TEXT, current_version: TEXT }, path);
  return {
    id: artifact.id,
    name: artifact.name,
    current_version: artifact.current_version,
    versions: rowsOf(readVersion)(what, artifact.versions, at(path, 'versions')),
  };
};

const EXTRACTION = { id: NON_EMPTY_TEXT, source_id: TEXT, revision: NUMBER, status: TEXT };
const EXTRACTION_FIELD = { id: NON_EMPTY_TEXT, label: TEXT, value: TEXT, uncertain: BOOLEAN };

export const readExtractions = rowsOf<Extraction>((what, value, path = '') => {
  const extraction = shaped(what, value, EXTRACTION, path);
  return {
    id: extraction.id,
    source_id: extraction.source_id,
    revision: extraction.revision,
    status: extraction.status,
    fields: shapedRows(what, extraction.fields, EXTRACTION_FIELD, at(path, 'fields')).map(({ id, label, value: text, uncertain }) => (
      { id, label, value: text, uncertain }
    )),
  };
});

export const readMaterialSettings: Reader<{ journal_enabled: boolean }> = (what, value, path = '') => (
  { journal_enabled: shaped(what, value, { journal_enabled: BOOLEAN }, path).journal_enabled }
);

const JOB = { id: NON_EMPTY_TEXT, status: TEXT, artifact_id: TEXT, version_id: optional(TEXT), error: optional(TEXT) };

export const readJob: Reader<Job> = (what, value, path = '') => {
  const { id, status, artifact_id, version_id, error } = shaped(what, value, JOB, path);
  return { id, status, artifact_id, ...(version_id === undefined ? {} : { version_id }), ...(error === undefined ? {} : { error }) };
};
export const readJobs = rowsOf(readJob);

const MATERIAL_SCOPE = { scope: TEXT, bot_scope: TEXT, current_account: optional(BOOLEAN) };

// 名单是给下拉框起名字的：读成空表，已有资料的会话就从下拉框里消失了。
export const readMaterialScopes = rowsOf<MaterialScope>((what, value, path = '') => {
  const row = shaped(what, value, MATERIAL_SCOPE, path);
  const owner = row.owner === undefined || row.owner === null
    ? row.owner as null | undefined
    : shaped(what, row.owner, MEMORY_OWNER, at(path, 'owner')) as unknown as MemoryOwner;
  return {
    scope: row.scope,
    bot_scope: row.bot_scope,
    ...(owner === undefined ? {} : { owner }),
    ...(row.current_account === undefined ? {} : { current_account: row.current_account }),
  };
});

export const readCapabilities: Reader<Capabilities> = (what, value, path = '') => {
  const { office_renderer, modules } = shaped(what, value, { office_renderer: BOOLEAN, modules: recordOf(BOOLEAN) }, path);
  return { office_renderer, modules };
};

const SPAN = { id: NON_EMPTY_TEXT, text: TEXT, citation: TEXT };

export const readSpans: Reader<{ results: Span[]; warnings: { message: string }[] }> = (what, value, path = '') => {
  const found = shaped(what, value, {}, path);
  return {
    results: shapedRows(what, found.results, SPAN, at(path, 'results')).map((span, index) => ({
      id: span.id,
      text: span.text,
      citation: span.citation,
      locator: { label: shaped(what, span.locator, { label: TEXT }, at(at(at(path, 'results'), index), 'locator')).label },
    })),
    warnings: shapedRows(what, found.warnings, WARNING, at(path, 'warnings')).map(({ message }) => ({ message })),
  };
};

export const readCitation: Reader<{ label: string; quote: string }> = (what, value, path = '') => {
  const { label, quote } = shaped(what, value, { label: TEXT, quote: TEXT }, path);
  return { label, quote };
};

const JOURNAL_HIT = { id: NON_EMPTY_TEXT, message_id: TEXT, sender_name: TEXT, timestamp: NUMBER, text: TEXT, truncated: BOOLEAN };

export const readJournalSearch: Reader<{ results: JournalHit[]; message?: string }> = (what, value, path = '') => {
  const found = shaped(what, value, { message: optional(TEXT) }, path);
  const results = shapedRows(what, found.results, JOURNAL_HIT, at(path, 'results')).map(({ id, message_id, sender_name, timestamp, text, truncated }) => (
    { id, message_id, sender_name, timestamp, text, truncated }
  ));
  return found.message === undefined ? { results } : { results, message: found.message };
};

export const readJournalMessage: Reader<{ text: string }> = (what, value, path = '') => (
  { text: shaped(what, value, { text: TEXT }, path).text }
);

export const readDelivery: Reader<{ message: string }> = (what, value, path = '') => (
  { message: shaped(what, value, { message: TEXT }, path).message }
);
