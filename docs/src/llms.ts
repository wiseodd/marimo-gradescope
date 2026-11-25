import { getCollection, type CollectionEntry } from 'astro:content';

// The pages in llms.txt, in header order. Each has a Markdown copy at `/<id>.md`.
export const PAGES = ['index', 'instructors', 'students'];

export async function pages(): Promise<CollectionEntry<'docs'>[]> {
	const entries = await getCollection('docs');
	return PAGES.map((id) => {
		const entry = entries.find((e) => e.id === id);
		if (!entry) throw new Error(`llms.txt: no docs page \`${id}\``);
		return entry;
	});
}
