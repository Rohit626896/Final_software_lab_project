
from src.paper.extractor import extract_paper


def main():
    result = extract_paper("sample.pdf")

    print("Paper:", result["filename"])
    print("Pages:", result["pages"])

    print("\nFirst 500 characters:")
    print(result["text"][:500])

    print("\nEmbedded hyperlinks:")
    for link in result["links"]:
        print(link)


if __name__ == "__main__":
    main()
