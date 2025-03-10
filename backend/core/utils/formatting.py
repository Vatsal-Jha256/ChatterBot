def format_markdown_response(response_text):
    """Ensure markdown formatting is preserved in the response"""
    # Preserve newlines
    formatted = response_text.replace('\\n', '\n')
    # Ensure proper spacing for markdown lists
    formatted = formatted.replace('\n-', '\n\n-')
    return formatted