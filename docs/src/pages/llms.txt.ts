import type { APIRoute } from 'astro';
import { pages } from '../llms';

const REPO = 'https://github.com/wiseodd/marimo-gradescope';

export const GET: APIRoute = async ({ site }) => {
	const base = site!.href.replace(/\/$/, '');
	const links = (await pages()).map(
		(e) => `- [${e.data.title}](${base}/${e.id}.md): ${e.data.description}`,
	);
	const text = [
		'# marimo-gradescope',
		'',
		'> Build Gradescope autograders from marimo notebooks.',
		'',
		'## Docs',
		'',
		...links,
		'',
		'## Example',
		'',
		`- [hw.py](${REPO}/raw/main/examples/demo/hw.py): a complete source notebook with solutions, public and hidden tests, and a written question`,
		'',
	].join('\n');

	return new Response(text, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
