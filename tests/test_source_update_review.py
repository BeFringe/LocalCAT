"""Display hunks never turn neighboring new/removed identities into a match."""
from collections import namedtuple
from types import SimpleNamespace
import unittest
from unittest import mock

import editor_source_update as display


Identity = namedtuple('Identity', 'document_id local_segment_id')
CATEGORIES = ('unchanged', 'source_changed', 'new', 'removed', 'ambiguous', 'unresolved')


def document(rows, *, doc_id='chapter', ref='chapter.rpy', digest='same'):
    return SimpleNamespace(document_id=doc_id, source_ref=ref, source_snapshot_digest=digest,
        source_segments=tuple(SimpleNamespace(local_segment_id=key, source=text) for key, text in rows),
        editing_overlay=tuple(SimpleNamespace(local_segment_id=key, target='译文 ' + key) for key, _ in rows))


def review(old, new, *, conflicts=()):
    """Synthetic owner classifications; source projection has no matching authority."""
    old_keys = {(d.document_id, s.local_segment_id): s.source for d in old for s in d.source_segments}
    new_keys = {(d.document_id, s.local_segment_id): s.source for d in new for s in d.source_segments}
    values = {category: [] for category in CATEGORIES}
    for key, text in old_keys.items():
        category = ('ambiguous' if key in conflicts else 'removed' if key not in new_keys else
                    'unchanged' if text == new_keys[key] else 'source_changed')
        values[category].append(Identity(*key))
    for key in new_keys.keys() - old_keys.keys():
        values['new'].append(Identity(*key))
    preview = SimpleNamespace(**{key + '_identities': tuple(value) for key, value in values.items()},
        required_decision_identities=tuple(values['removed'] + values['ambiguous'] + values['unresolved']))
    return display.source_update_review(preview, SimpleNamespace(documents=old), SimpleNamespace(documents=new))


class SourceUpdateReviewTests(unittest.TestCase):
    def test_typo_with_changed_identity_is_one_display_block_but_still_new_removed(self):
        old = document([('a', 'Before'), ('old', 'The ship had had sailed.'), ('z', 'After')])
        new = document([('a', 'Before'), ('new', 'The ship had sailed.'), ('z', 'After')], digest='new')
        view = review((old,), (new,))
        self.assertEqual(len(view.blocks), 1)
        block = view.blocks[0]
        self.assertEqual([view.items[i].category for i in block.item_indices], ['new', 'removed'])
        self.assertEqual((block.old[0].segment_number, block.new[0].segment_number), (2, 2))
        self.assertEqual(block.old[0].source, 'The ship had had sailed.')
        self.assertEqual(block.old[0].target, '译文 old')
        self.assertEqual([item.category for item in view.required_items], ['removed'])

    def test_four_separated_replacements_and_same_id_change_follow_document_order(self):
        old_rows, new_rows = [('anchor0', 'Same')], [('anchor0', 'Same')]
        for number in range(4):
            old_rows.extend([(f'old{number}', 'Old phrase'), (f'anchor{number + 1}', 'Same')])
            new_rows.extend([(f'new{number}', 'New phrase'), (f'anchor{number + 1}', 'Same')])
        view = review((document(old_rows),), (document(new_rows),))
        self.assertEqual(len(view.blocks), 4)
        self.assertEqual([block.old[0].segment_number for block in view.blocks], [2, 4, 6, 8])
        new_rows[2] = ('anchor1', 'Changed at stable identity')
        view = review((document(old_rows),), (document(new_rows),))
        self.assertEqual(len(view.blocks), 5)
        self.assertEqual([view.items[i].category for i in view.blocks[1].item_indices], ['source_changed'])
        self.assertEqual(view.blocks[1].old[0].source, 'Same')
        self.assertEqual(view.blocks[1].new[0].source, 'Changed at stable identity')

    def test_multiple_replacements_are_not_paired_and_same_basename_stays_separate(self):
        old = tuple(document([('a', 'Anchor'), ('x', 'X'), ('y', 'Y'), ('z', 'End')],
                             doc_id=key, ref=f'{key}/intro.rpy') for key in ('one', 'two'))
        new = tuple(document([('a', 'Anchor'), ('p', 'P'), ('q', 'Q'), ('z', 'End')],
                             doc_id=key, ref=f'{key}/intro.rpy') for key in ('one', 'two'))
        view = review(old, new)
        self.assertEqual(len(view.blocks), 2)
        self.assertEqual([(len(block.old), len(block.new)) for block in view.blocks], [(2, 2), (2, 2)])
        self.assertEqual([block.old[0].source_ref for block in view.blocks], ['one/intro.rpy', 'two/intro.rpy'])

    def test_reordered_anchors_use_one_conservative_block_without_losing_changes(self):
        old = document([('a', 'A'), ('x', 'X'), ('b', 'B'), ('y', 'Y'), ('c', 'C')])
        new = document([('b', 'B'), ('p', 'P'), ('a', 'A'), ('q', 'Q'), ('c', 'C')])
        view = review((old,), (new,))
        self.assertEqual(len(view.blocks), 1)
        self.assertEqual([text.source for text in view.blocks[0].old], ['X', 'Y'])
        self.assertEqual([text.source for text in view.blocks[0].new], ['P', 'Q'])

    def test_empty_diff_distinguishes_snapshot_and_path_changes(self):
        old = document([('a', 'Source')])
        same = review((old,), (document([('a', 'Source')]),))
        self.assertEqual(same.blocks, ())
        self.assertTrue(same.templates_identical)
        for changed in (document([('a', 'Source')], digest='different'),
                        document([('a', 'Source')], ref='renamed.rpy')):
            view = review((old,), (changed,))
            self.assertEqual(view.blocks, ())
            self.assertFalse(view.templates_identical)

    def test_new_only_removed_only_and_conflict_keep_complete_display_coverage(self):
        old = document([('a', 'A'), ('remove', 'Removed'), ('b', 'B'), ('conflict', 'Conflict')])
        new = document([('a', 'A'), ('b', 'B'), ('new', 'New'), ('conflict', 'Conflict')])
        view = review((old,), (new,), conflicts=(('chapter', 'conflict'),))
        indices = [index for block in view.blocks for index in block.item_indices]
        self.assertCountEqual(indices, [i for i, item in enumerate(view.items) if item.category != 'unchanged'])
        self.assertEqual(len(indices), len(set(indices)))

    def test_chapters_include_zero_changes_paths_and_original_document_order(self):
        old = (document([('a', 'A')], doc_id='zero', ref='zero/intro.rpy'),
               document([('a', 'A'), ('x', 'Old'), ('z', 'Z')], doc_id='changed', ref='one/intro.rpy'),
               document([('a', 'A')], doc_id='renamed', ref='two/intro.rpy'))
        new = (document([('a', 'A')], doc_id='zero', ref='zero/intro.rpy'),
               document([('a', 'A'), ('y', 'New'), ('z', 'Z')], doc_id='changed', ref='one/intro.rpy', digest='new'),
               document([('a', 'A')], doc_id='renamed', ref='three/intro.rpy'))
        view = review(old, new)
        self.assertTrue(hasattr(view, 'chapters'), '预览须保留零差异章及逐章计数')
        self.assertEqual([c.document_id for c in view.chapters], ['zero', 'changed', 'renamed'])
        self.assertEqual([c.block_indices for c in view.chapters], [(), (0,), ()])
        self.assertEqual(dict(view.chapters[1].counts), dict(unchanged=2, source_changed=0,
                          new=1, removed=1, ambiguous=0, unresolved=0))
        self.assertEqual((view.chapters[2].old_source_ref, view.chapters[2].new_source_ref),
                         ('two/intro.rpy', 'three/intro.rpy'))
        self.assertEqual([c.templates_identical for c in view.chapters], [True, False, False])

    def test_context_is_same_unchanged_identity_adjacent_in_both_sequences(self):
        old = document([('a', '<before> & body'), ('x', 'Old'), ('z', 'After')])
        new = document([('a', '<before> & body'), ('y', 'New'), ('z', 'After')])
        view = review((old,), (new,))
        block = view.blocks[0]
        self.assertTrue(hasattr(block, 'before'), '差异应投影前后稳定上下文')
        self.assertEqual((block.before.old.source, block.before.new.source), ('<before> & body',) * 2)
        self.assertEqual((block.after.old.segment_number, block.after.new.segment_number), (3, 3))
        self.assertTrue(all(view.items[i].category != 'unchanged' for i in block.item_indices))

    def test_insert_delete_and_edge_context_positions(self):
        variants = (
            ([('a', 'A'), ('z', 'Z')], [('a', 'A'), ('x', 'X'), ('z', 'Z')], (1, 1), (2, 3)),
            ([('a', 'A'), ('x', 'X'), ('z', 'Z')], [('a', 'A'), ('z', 'Z')], (1, 1), (3, 2)),
            ([('x', 'Old'), ('z', 'Z')], [('x', 'New'), ('z', 'Z')], None, (2, 2)),
            ([('a', 'A'), ('x', 'Old')], [('a', 'A'), ('x', 'New')], (1, 1), None),
        )
        for old_rows, new_rows, before, after in variants:
            with self.subTest(old=old_rows, new=new_rows):
                block = review((document(old_rows),), (document(new_rows),)).blocks[0]
                self.assertTrue(hasattr(block, 'before'), '插入／删除区间也须准确展示上下文')
                for context, positions in ((block.before, before), (block.after, after)):
                    self.assertEqual(None if context is None else
                        (context.old.segment_number, context.new.segment_number), positions)

    def test_context_does_not_cross_another_change_conflict_or_reorder(self):
        old = document([('a', 'A'), ('x', 'Old'), ('removed', 'Removed'), ('z', 'Z')])
        new = document([('a', 'A'), ('x', 'Changed'), ('new', 'New'), ('z', 'Z')])
        blocks = review((old,), (new,)).blocks
        self.assertEqual(len(blocks), 2)
        self.assertTrue(hasattr(blocks[0], 'before'), '上下文必须由可靠邻接投影')
        self.assertIsNone(blocks[0].after)
        self.assertIsNone(blocks[1].before)
        reordered = document([('z', 'Z'), ('x', 'Changed'), ('new', 'New'), ('a', 'A')])
        block = review((old,), (reordered,)).blocks[0]
        self.assertIsNone(block.before)
        self.assertIsNone(block.after)
        conflicted = review((old,), (new,), conflicts=(('chapter', 'a'),))
        self.assertIsNone(conflicted.blocks[1].before)

    def test_moved_conflict_is_displayed_once_without_context_guessing(self):
        old = document([('a', 'A'), ('conflict', 'Conflict'), ('b', 'B'), ('x', 'Old'), ('z', 'Z')])
        new = document([('a', 'A'), ('b', 'B'), ('conflict', 'Conflict'), ('y', 'New'), ('z', 'Z')])
        view = review((old,), (new,), conflicts=(('chapter', 'conflict'),))
        covered = [i for block in view.blocks for i in block.item_indices]
        self.assertEqual(len(covered), len(set(covered)))
        self.assertCountEqual(covered, [i for i, item in enumerate(view.items) if item.category != 'unchanged'])
        self.assertIsNone(view.blocks[1].before)

    def test_word_changes_preserve_all_text_and_bound_long_or_repetitive_input(self):
        result = display.source_update_word_diff('The ship had had sailed.', 'The ship had sailed.')
        self.assertTrue(result.highlighted)
        self.assertEqual(''.join(text for text, _ in result.old), 'The ship had had sailed.')
        self.assertEqual(''.join(text for text, _ in result.new), 'The ship had sailed.')
        self.assertIn('had', ''.join(text for text, changed in result.old if changed))
        for old, new in (('a' * 30000, 'b' * 30000), ('word ' * 3000, 'words ' * 3000)):
            with mock.patch.object(display, 'SequenceMatcher', side_effect=AssertionError('unbounded diff')):
                result = display.source_update_word_diff(old, new)
            self.assertFalse(result.highlighted)
            self.assertEqual(result.old, ((old, False),))
            self.assertEqual(result.new, ((new, False),))


if __name__ == '__main__':
    unittest.main()
