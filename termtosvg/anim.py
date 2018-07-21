import io
import logging
import os
import pkgutil
from collections import namedtuple
from itertools import groupby
from typing import Dict, List, Iterable, Iterator, Union, Tuple, Any

import pyte.graphics
import pyte.screens
from lxml import etree

# Ugliest hack: Replace the first 16 colors rgb values by their names so that termtosvg can
# distinguish FG_BG_256[0] (which defaults to black #000000 but can be styled with themes)
# from FG_BG_256[16] (which is also black #000000 but should be displayed as is).
_COLORS = ['black', 'red', 'green', 'brown', 'blue', 'magenta', 'cyan', 'white']
_BRIGHTCOLORS = ['bright{}'.format(color) for color in _COLORS]
ALL_COLORS = _COLORS + _BRIGHTCOLORS
pyte.graphics.FG_BG_256 = ALL_COLORS + pyte.graphics.FG_BG_256[16:]

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())

# Id for the very last SVG animation. This is used to make the first animations start when the
# last one ends (animation looping)
LAST_ANIMATION_ID = 'anim_last'

# XML namespaces
SVG_NS = 'http://www.w3.org/2000/svg'
XLINK_NS = 'http://www.w3.org/1999/xlink'
TERMTOSVG_NS = 'https://github.com/nbedos/termtosvg'


class TemplateError(Exception):
    pass


_CharacterCell = namedtuple('_CharacterCell', ['text', 'color', 'background_color', 'bold'])
_CharacterCell.__doc__ = 'Representation of a character cell'
_CharacterCell.text.__doc__ = 'Text content of the cell'
_CharacterCell.bold.__doc__ = 'Bold modificator flag'
_CharacterCell.color.__doc__ = 'Color of the text'
_CharacterCell.background_color.__doc__ = 'Background color of the cell'


class CharacterCell(_CharacterCell):
    @classmethod
    def from_pyte(cls, char, palette):
        # type: (pyte.screens.Char, Dict[Any, str]) -> CharacterCell
        """Create a CharacterCell from a pyte character"""
        # Map named colors to their respective number
        color_numbers = dict(zip(ALL_COLORS, range(len(ALL_COLORS))))
        if char.fg == 'default':
            text_color = palette['foreground']
        else:
            if char.bold and not str(char.fg).startswith('bright'):
                search_color = 'bright{}'.format(char.fg)
            else:
                search_color = char.fg

            if search_color in color_numbers:
                # NAMED COLORS
                if color_numbers[search_color] in palette:
                    # Case for color numbers < 8 (since the palette has at least the first 8 colors)
                    # or for 16-color palette (all named colors in the palette)
                    color_number = color_numbers[search_color]
                else:
                    # Case for color numbers >= 8 and 8-color palette: fallback to non bright color
                    color_number = color_numbers[search_color] % 8
                text_color = palette[color_number]
            elif len(char.fg) == 6:
                # HEXADECIMAL COLORS
                # raise ValueError if char.fg is not an hexadecimal number
                int(char.fg, 16)
                text_color = '#{}'.format(char.fg)
            else:
                raise ValueError('Invalid foreground color: {}'.format(char.fg))

        if char.bg == 'default':
            background_color = palette['background']
        elif char.bg in color_numbers:
            # Named colors
            background_color = palette[color_numbers[char.bg]]
        elif len(char.bg) == 6:
            # Hexadecimal colors
            # raise ValueError if char.bg is not an hexadecimal number
            int(char.bg, 16)
            background_color = '#{}'.format(char.bg)
        else:
            raise ValueError('Invalid background color')

        if char.reverse:
            text_color, background_color = background_color, text_color

        return CharacterCell(char.data, text_color, background_color, char.bold)


CharacterCellConfig = namedtuple('CharacterCellConfig', ['width', 'height', 'text_color',
                                                         'background_color'])
CharacterCellLineEvent = namedtuple('CharacterCellLineEvent', ['row', 'line', 'time', 'duration'])
CharacterCellRecord = Union[CharacterCellConfig, CharacterCellLineEvent]


class ConsecutiveWithSameAttributes:
    """Callable to be used as a key for itertools.groupby to group together consecutive elements
    of a list with the same attributes"""
    def __init__(self, attributes):
        self.group_index = None
        self.last_index = None
        self.attributes = attributes
        self.last_key_attributes = None

    def __call__(self, arg):
        index, obj = arg
        key_attributes = {name: getattr(obj, name) for name in self.attributes}
        if self.last_index != index - 1 or self.last_key_attributes != key_attributes:
            self.group_index = index
        self.last_index = index
        self.last_key_attributes = key_attributes
        return self.group_index, key_attributes


def make_rect_tag(column, length, height, cell_width, cell_height, background_color):
    # type: (int, int, int, int, int, str) -> etree.ElementBase
    attributes = {
        'x': str(column * cell_width),
        'y': str(height),
        'width': str(length * cell_width),
        'height': str(cell_height),
        'fill': background_color
    }
    rect_tag = etree.Element('rect', attributes)
    return rect_tag


def _render_line_bg_colors(screen_line, height, cell_height, cell_width, default_bg_color):
    # type: (Dict[int, CharacterCell], int, int, int) -> List[etree.ElementBase]
    """Return a list of 'rect' tags representing the background of 'screen_line'

    If consecutive cells have the same background color, a single 'rect' tag is returned for all
    these cells.
    If a cell background uses default_bg_color, no 'rect' will be generated for this cell since
    the default background is always displayed.

    :param screen_line: Mapping between column numbers and CharacterCells
    :param height: Vertical position of the line on the screen in pixels
    :param cell_height: Height of the a character cell in pixels
    :param cell_width: Width of a character cell in pixels
    :param default_bg_color: Default background color
    """
    non_default_bg_cells = [(column, cell) for (column, cell) in sorted(screen_line.items())
                            if cell.background_color != default_bg_color]

    key = ConsecutiveWithSameAttributes(['background_color'])
    rect_tags = [make_rect_tag(column, len(list(group)), height, cell_width, cell_height,
                               attributes['background_color'])
                 for (column, attributes), group in groupby(non_default_bg_cells, key)]

    return rect_tags


def make_text_tag(column, attributes, text, cell_width):
    # type: (List[Tuple[int, CharacterCell]], Dict[str, str], str, int) -> etree.ElementBase
    text_tag_attributes = {
        'x': str(column * cell_width),
        'textLength': str(len(text) * cell_width),
        'lengthAdjust': 'spacingAndGlyphs',
        'fill': attributes['color']
    }
    if attributes['bold']:
        text_tag_attributes['font-weight'] = 'bold'

    text_tag = etree.Element('text', text_tag_attributes)
    # Replace usual spaces with unbreakable spaces so that indenting the SVG does not mess up
    # the whole animation; this is somewhat better than the 'white-space: pre' CSS option
    text_tag.text = text.replace(' ', '\u00A0')
    return text_tag


def _render_characters(screen_line, cell_width):
    # type: (Dict[int, CharacterCell], int) -> List[etree.ElementBase]
    """Return a list of 'text' elements representing the line of the screen

    Consecutive characters with the same styling attributes (text color and font weight) are
    grouped together in a single text element.

    :param screen_line: Mapping between column numbers and characters
    :param cell_width: Width of a character cell in pixels
    """
    line = [(col, char) for (col, char) in sorted(screen_line.items())]
    key = ConsecutiveWithSameAttributes(['color', 'bold'])
    text_tags = [make_text_tag(column, attributes, ''.join(c.text for _, c in group), cell_width)
                 for (column, attributes), group in groupby(line, key)]

    return text_tags


def build_style_tag(font, font_size, background_color):
    # type: (str, int, str) -> etree.ElementBase
    css = {
        # Apply this style to each and every element since we are using coordinates that
        # depend on the size of the font
        '*': {
            'font-family': '"{}", monospace'.format(font),
            'font-style': 'normal',
            'font-size': '{}px'.format(font_size),
        },
        'text': {
            'dominant-baseline': 'text-before-edge',
        },
        '.background': {
            'fill': background_color,
        },
    }

    style_attributes = {
        'type': "text/css"
    }
    style_tag = etree.Element('style', style_attributes)
    style_tag.text = etree.CDATA(_serialize_css_dict(css))
    return style_tag


_BG_RECT_TAG_ATTRIBUTES = {
    'class': 'background',
    'height': '100%',
    'width': '100%',
    'x': '0',
    'y': '0'
}
BG_RECT_TAG = etree.Element('rect', _BG_RECT_TAG_ATTRIBUTES)


def validate_svg(svg_file):
    """Validate an SVG file against the latest version of SVG 1.1 Document Type Definition"""
    data = pkgutil.get_data(__name__, 'data/svg11-flat-20110816.dtd')
    dtd = etree.DTD(io.BytesIO(data))

    try:
        tree = etree.parse(svg_file)
        root = tree.getroot()
        is_valid = dtd.validate(root)
    except etree.Error as exc:
        raise ValueError('Invalid SVG file') from exc

    if not is_valid:
        reason = dtd.error_log.filter_from_errors()[0]
        raise ValueError('Invalid SVG file: {}'.format(reason))


def make_animated_group(records, time, duration, cell_height, cell_width, default_bg_color, defs):
    # type: (Iterable[CharacterCellLineEvent], int, int, int, int, str, Dict[str, etree.ElementBase]) -> Tuple[etree.ElementBase, Dict[str, etree.ElementBase]]
    """Return a group element containing an SVG version of the provided records. This group is
    animated, that is to say displayed then removed according to the timing arguments.

    :param records: List of lines that should be included in the group
    :param time: Time the group should appear on the screen (milliseconds)
    :param duration: Duration of the appearance on the screen (milliseconds)
    :param cell_height: Height of a character cell in pixels
    :param cell_width: Width of a character cell in pixels
    :param default_bg_color: Default background color
    :param defs: Existing definitions
    :return: A tuple consisting of the animated group and the new definitions
    """
    animation_group_tag = etree.Element('g', attrib={'display': 'none'})
    new_definitions = {}
    for event_record in records:
        # Background elements
        rect_tags = _render_line_bg_colors(screen_line=event_record.line,
                                           height=event_record.row * cell_height,
                                           cell_height=cell_height,
                                           cell_width=cell_width,
                                           default_bg_color=default_bg_color)
        for tag in rect_tags:
            animation_group_tag.append(tag)

        # Group text elements for the current line into text_group_tag
        text_group_tag = etree.Element('g')
        text_tags = _render_characters(event_record.line, cell_width)
        for tag in text_tags:
            text_group_tag.append(tag)

        # Find or create a definition for text_group_tag
        text_group_tag_str = etree.tostring(text_group_tag)
        if text_group_tag_str in defs:
            group_id = defs[text_group_tag_str].attrib['id']
        elif text_group_tag_str in new_definitions:
            group_id = new_definitions[text_group_tag_str].attrib['id']
        else:
            group_id = 'g{}'.format(len(defs) + len(new_definitions) + 1)
            assert group_id not in defs.values() and group_id not in new_definitions.values()
            text_group_tag.attrib['id'] = group_id
            new_definitions[text_group_tag_str] = text_group_tag

        # Add a reference to the definition of text_group_tag with a 'use' tag
        use_attributes = {
            '{{{}}}href'.format(XLINK_NS): '#{}'.format(group_id),
            'y': str(event_record.row * cell_height),
        }
        use_tag = etree.Element('use', use_attributes)
        animation_group_tag.append(use_tag)

    # Finally, add an animation tag so that the whole group goes from 'display: none' to
    # 'display: inline' at the time the line should appear on the screen
    if time == 0:
        # Animations starting at 0ms should also start when the last animation ends (looping)
        begin_time = '0ms; {id}.end'.format(id=LAST_ANIMATION_ID)
    else:
        begin_time = '{time}ms; {id}.end+{time}ms'.format(time=time, id=LAST_ANIMATION_ID)
    attributes = {
        'attributeName': 'display',
        'from': 'inline',
        'to': 'inline',
        'begin': begin_time,
        'dur': '{}ms'.format(duration),
        'fill': 'remove',
    }

    animation = etree.Element('animate', attributes)
    animation_group_tag.append(animation)

    return animation_group_tag, new_definitions


def render_animation(records, filename, font, font_size=14, cell_width=8, cell_height=17):
    root = _render_animation(records, font, font_size, cell_width, cell_height)
    with open(filename, 'wb') as output_file:
        output_file.write(etree.tostring(root))


def resize_template(template, columns, rows, cell_width, cell_height):
    # type: (str, int, int, int, int) -> etree.ElementBase
    def scale(element, template_columns, template_rows, columns, rows):
        try:
            viewbox = element.attrib['viewBox'].replace(',', ' ').split()
        except KeyError:
            raise TemplateError('Missing "viewBox" for element "{}"'.format(element))

        vb_min_x, vb_min_y, vb_width, vb_height = [int(n) for n in viewbox]
        vb_width += cell_width * (columns - template_columns)
        vb_height += cell_height * (rows - template_rows)
        element.attrib['viewBox'] = ' '.join([str(n) for n in (vb_min_x, vb_min_y, vb_width, vb_height)])

        scalable_attributes = {
            'width': cell_width * (columns - template_columns),
            'height': cell_height * (rows - template_rows)
        }

        for attribute, delta in scalable_attributes.items():
            if attribute in element.attrib:
                try:
                    element.attrib[attribute] = str(int(element.attrib[attribute]) + delta)
                except ValueError:
                    raise TemplateError('"{}" attribute of {} must be in user units'
                                        .format(attribute, element))
        return element

    data = pkgutil.get_data(__name__, template)

    try:
        tree = etree.parse(io.BytesIO(data))
        root = tree.getroot()
    except etree.Error as exc:
        raise TemplateError('Invalid template') from exc

    # Extract the screen geometry which is saved in a private data portion of the template
    settings = root.find('.//{{{}}}defs/{{{}}}template_settings'.format(SVG_NS, TERMTOSVG_NS))
    if settings is None:
        raise TemplateError('Missing "template_settings" element in definitions')

    geometry = settings.find('{{{}}}screen_geometry[@columns][@rows]'.format(TERMTOSVG_NS))
    if geometry is None:
        raise TemplateError('Missing "screen_geometry" element in "template_settings"')

    attributes_err_msg = ('Missing or invalid "columns" or "rows" attribute for element '
                          '"screen_geometry": expected positive integers')
    try:
        template_columns = int(geometry.attrib['columns'])
        template_rows = int(geometry.attrib['rows'])
    except (KeyError, ValueError) as exc:
        raise TemplateError(attributes_err_msg) from exc

    if template_rows <= 0 or template_columns <= 0:
        raise TemplateError(attributes_err_msg)

    # Scale the viewBox of the root svg element based on the size of the screen and the size
    # registered in the template
    scale(root, template_columns, template_rows, columns, rows)

    # Also scale the viewBox of the svg element with id 'screen'
    screen = root.find('.//{{{}}}svg[@id="screen"]'.format(SVG_NS))
    if screen is None:
        raise TemplateError('svg element with id "screen" not found')
    scale(screen, template_columns, template_rows, columns, rows)

    # Remove termtosvg private data so that the template can be validated against the DTD of SVG 1.1
    settings.getparent().remove(settings)

    return root


def _render_animation(records, font, font_size, cell_width, cell_height):
    # type: (Iterable[CharacterCellRecord], str, int, int, int) -> etree.ElementBase
    # Read header record and add the corresponding information to the SVG
    if not isinstance(records, Iterator):
        records = iter(records)
    header = next(records)

    #root = resize_template('data/templates/plain.svg', header.width, header.height, cell_width, cell_height)
    root = resize_template('data/templates/carbon.svg', header.width, header.height, cell_width,
                           cell_height)

    svg_screen_tag = root.find('.//{http://www.w3.org/2000/svg}svg[@id="screen"]')
    if svg_screen_tag is None:
        raise ValueError('Missing tag: <svg id="screen" ...>...</svg>')

    for child in svg_screen_tag.getchildren():
        svg_screen_tag.remove(child)

    def_tag = etree.SubElement(svg_screen_tag, 'defs')
    style_tag = build_style_tag(font, font_size, header.background_color)
    def_tag.append(style_tag)
    svg_screen_tag.append(BG_RECT_TAG)

    # Process event records
    def by_time(record: CharacterCellRecord) -> Tuple[int, int]:
        return record.time, record.duration

    definitions = {}
    last_animated_group = None
    animation_duration = None
    for (line_time, line_duration), record_group in groupby(records, key=by_time):
        animated_group, new_defs = make_animated_group(records=record_group,
                                                       time=line_time,
                                                       duration=line_duration,
                                                       cell_height=cell_height,
                                                       cell_width=cell_width,
                                                       default_bg_color=header.background_color,
                                                       defs=definitions)
        definitions.update(new_defs)
        for definition in new_defs.values():
            def_tag.append(definition)

        svg_screen_tag.append(animated_group)
        last_animated_group = animated_group
        animation_duration = line_time + line_duration

    # Add id attribute to the last 'animate' tag so that it can be refered to by the first
    # animations (enables animation looping)
    if last_animated_group is not None:
        animate_tags = last_animated_group.findall('animate')
        assert len(animate_tags) == 1
        animate_tags.pop().attrib['id'] = LAST_ANIMATION_ID

    add_css_variables(root, header.text_color, header.background_color, animation_duration)

    return root


def add_css_variables(root, foreground_color, background_color, animation_duration):
    # type: (etree.ElementBase, str, str, int) -> etree.ElementBase
    try:
        style = root.find('.//{{{}}}defs/{{{}}}style[@class="generated"]'.format(SVG_NS, SVG_NS))
    except etree.Error as exc:
        raise TemplateError('Invalid template') from exc

    if style is None:
        raise TemplateError('Missing <style class="generated" ...> element in "defs"')

    css = {
        ':root': {
            '--foreground-color':  foreground_color,
            '--background-color':  background_color,
            '--animation-duration': '{}ms'.format(animation_duration)
        }
    }

    style.text = etree.CDATA(_serialize_css_dict(css))
    return root


def _serialize_css_dict(css):
    # type: (Dict[str, Dict[str, str]]) -> str
    def serialize_css_item(item):
        return '; '.join('{}: {}'.format(prop, item[prop]) for prop in item)

    items = ['{} {{{}}}'.format(item, serialize_css_item(css[item])) for item in css]
    return os.linesep.join(items)
