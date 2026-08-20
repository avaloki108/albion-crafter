from albion_crafter.core.models import Item

ITEMS: dict[str, Item] = {
    "T4_MAIN_SWORD": Item("T4_MAIN_SWORD", "Sample T4 Broadsword", 4, category="weapon"),
    "T5_MAIN_AXE": Item("T5_MAIN_AXE", "Sample T5 Battleaxe", 5, category="weapon"),
    "T5_BAG": Item("T5_BAG", "Sample T5 Bag", 5, category="armor"),
    "T4_METALBAR": Item("T4_METALBAR", "T4 Metal Bar", 4, category="resource"),
    "T4_PLANKS": Item("T4_PLANKS", "T4 Planks", 4, category="resource"),
    "T5_METALBAR": Item("T5_METALBAR", "T5 Metal Bar", 5, category="resource"),
    "T5_PLANKS": Item("T5_PLANKS", "T5 Planks", 5, category="resource"),
    "T5_LEATHER": Item("T5_LEATHER", "T5 Leather", 5, category="resource"),
    "T5_CLOTH": Item("T5_CLOTH", "T5 Cloth", 5, category="resource"),
    "T5_ARTEFACT_MAIN_AXE_KEEPER": Item(
        "T5_ARTEFACT_MAIN_AXE_KEEPER", "Sample Axe Artifact", 5, category="artifact"
    ),
}
