from hermes_cli import design_board_tailwind as tw


def test_non_tailwind_html_is_returned_unchanged():
    html = "<h1 class='text-red-500'>local css elsewhere</h1>"
    assert tw.inline_tailwind_cdn_mockup_html(html) == html


def test_tailwind_script_regex_handles_single_quotes():
    assert tw.has_tailwind_cdn("<script src='https://cdn.tailwindcss.com'></script>")


def test_tailwind_script_regex_handles_double_quotes_with_query():
    assert tw.has_tailwind_cdn('<script defer src="https://cdn.tailwindcss.com?plugins=forms"></script>')
