from app.domain.reports import cluster_shadow_drafts


def test_same_prefix_does_not_merge_different_questions():
    prefix = '背景资料' * 30
    rows = [{'id': 1, 'shadow': True, 'question': prefix + '是否允许退款'},
            {'id': 2, 'shadow': True, 'question': prefix + '是否允许换货'}]
    assert len(cluster_shadow_drafts(rows)) == 2


def test_programming_operators_are_part_of_the_question():
    rows = [{'id': 1, 'shadow': True, 'question': 'C++怎么安装？'},
            {'id': 2, 'shadow': True, 'question': 'C怎么安装？'}]
    assert len(cluster_shadow_drafts(rows)) == 2
