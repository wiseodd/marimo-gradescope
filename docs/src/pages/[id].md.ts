import type { APIRoute, GetStaticPaths } from 'astro';
import type { CollectionEntry } from 'astro:content';
import { pages } from '../llms';

export const getStaticPaths: GetStaticPaths = async () =>
	(await pages()).map((entry) => ({ params: { id: entry.id }, props: { entry } }));

// The page's Markdown source under its title, for agents that read llms.txt.
export const GET: APIRoute = ({ props }) => {
	const { entry } = props as { entry: CollectionEntry<'docs'> };
	const text = `# ${entry.data.title}\n\n${entry.body!.trim()}\n`;

	return new Response(text, { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } });
};
