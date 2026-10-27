export type Ref = { chunk_id: string; quote: string };
export type Chunk = { id: string; page: number; text: string };
export type Book = {
  id: string;
  filename: string;
  page_count: number;
  section_count: number;
  unavailable_pages: number[];
};
export type Section = {
  id: string;
  title: string;
  start_page: number;
  end_page: number;
};
export type Index = {
  id: string;
  created_at: string;
  status: "ready" | "building";
  pipeline?: string | null;
  indexed_sections?: string[];
};
export type Point = {
  key: string;
  title: string;
  description: string;
  section_id: string;
  source_refs: Ref[];
};
export type Item = {
  description: string;
  kind: string;
  point_keys: string[];
  source_refs: Ref[];
};
export type Catalog = { book: Book; sections: Section[]; indexes: Index[] };
export type Coverage = {
  task_id: string;
  section_id: string;
  status: string;
  total: number;
  checked: number;
  with_relations: number;
  without_relations: number;
  unexpanded: number;
  last_error: string | null;
};
export type Chapter = {
  quality?: "pending" | "reviewed" | "legacy";
  exclusions?: {
    chunk_id: string;
    start: number;
    end: number;
    reason: string;
    quote: string;
  }[];
  section: Section;
  index: Index | null;
  source: "snapshot" | "checkpoints" | "original";
  points: Point[];
  items: Item[];
  summary: string | null;
  saved_batches: number;
  last_step: string | null;
  last_error: string | null;
  coverage: Coverage[];
  coverage_available: boolean;
  read_at: string;
};
export type Relation = {
  source: string;
  target: string;
  kind: "prerequisite" | "related";
  reason: string;
};
export type Pair = {
  other_key: string;
  status: "with_relations" | "without_relations" | "unchecked";
  task_id: string;
  section_id: string;
};
export type PointView = {
  point: Point;
  neighbors: Point[];
  relations: Relation[];
  pairs: Pair[];
  coverage_available: boolean;
  missing_keys: string[];
};
export type Page = {
  page: number;
  page_count: number;
  unavailable: boolean;
  chunks: Chunk[];
};
